"""SSH 密钥工具：公钥推导、指纹计算与密钥对生成。

部署到私有仓库最麻烦的一步是「生成密钥对并把公钥登记到 GitHub」。把这一步
搬进服务里，用户只需点一次按钮、复制一次公钥即可，避免了手工运行
``ssh-keygen``、找错文件、或把私钥和公钥搞反。

三个职责：

* :func:`public_key_line` —— 从**私钥**反推公钥。OpenSSH 私钥文件
  (``-----BEGIN OPENSSH PRIVATE KEY-----``) 的公开部分以明文存放，
  因此即使私钥带口令也能读到公钥，无需解密。
* :func:`fingerprint` —— 计算 ``SHA256:...`` 指纹，与 ``ssh-keygen -lf``
  及 GitHub 界面显示的一致，用于核对登记的是不是同一把钥匙。
* :func:`generate_keypair` —— 调用 ``ssh-keygen`` 生成密钥对。

只用标准库解析密钥，不引入额外依赖；生成走 :mod:`app.executor`，与其它
子进程共享超时与清理逻辑。
"""

from __future__ import annotations

import base64
import hashlib
import shutil
import struct
import tempfile
from pathlib import Path

from .executor import run_command

# OpenSSH 私钥文件的魔术前缀；其后依次是 ciphername、kdfname、kdfoptions。
_OPENSSH_MAGIC = b"openssh-key-v1\x00"

# 支持生成的密钥类型。ed25519 是当前推荐，RSA 供老旧环境兼容。
KEY_TYPES = {
    "ed25519": "ED25519（推荐）",
    "rsa": "RSA 4096（兼容旧环境）",
}

_BEGIN_MARKERS = ("-----BEGIN OPENSSH PRIVATE KEY-----", "-----BEGIN RSA PRIVATE KEY-----",
                  "-----BEGIN EC PRIVATE KEY-----", "-----BEGIN DSA PRIVATE KEY-----",
                  "-----BEGIN PRIVATE KEY-----")


def looks_like_private_key(text: str) -> bool:
    """粗略判断文本是否像一份私钥，用于给出更贴合的中文报错。"""
    stripped = (text or "").strip()
    return any(stripped.startswith(marker) for marker in _BEGIN_MARKERS)


def validate_private_key(text: str) -> str | None:
    """返回不可用的原因；``None`` 表示看起来可用。"""
    stripped = (text or "").strip()
    if not stripped:
        return "请粘贴私钥内容"
    if not looks_like_private_key(stripped):
        return "私钥格式不正确：应以 -----BEGIN ... PRIVATE KEY----- 开头"
    if "PRIVATE KEY-----" not in stripped:
        return "私钥内容不完整：缺少结尾行"
    return None


def _read_string(buf: bytes, pos: int) -> tuple[bytes, int]:
    """读取 OpenSSH 格式里的长度前缀字符串。"""
    if pos + 4 > len(buf):
        raise ValueError("数据不完整")
    (length,) = struct.unpack(">I", buf[pos:pos + 4])
    start = pos + 4
    end = start + length
    if end > len(buf):
        raise ValueError("数据不完整")
    return buf[start:end], end


def public_blob(private_key: str) -> bytes:
    """从 OpenSSH 私钥中取出公钥二进制块（ssh-ed25519/ssh-rsa 的 wire 格式）。

    失败返回空字节串——调用方据此回退到其它展示方式，不应因此报错中断。
    """
    lines = [
        line.strip()
        for line in (private_key or "").splitlines()
        if line.strip() and not line.startswith("-----")
    ]
    if not lines:
        return b""
    try:
        raw = base64.b64decode("".join(lines))
    except (ValueError, TypeError):
        return b""

    if not raw.startswith(_OPENSSH_MAGIC):
        # 传统 PEM 私钥（如 BEGIN RSA PRIVATE KEY）不含公钥，无法直接推导。
        return b""
    pos = len(_OPENSSH_MAGIC)
    try:
        for _ in range(3):  # ciphername、kdfname、kdfoptions
            _, pos = _read_string(raw, pos)
        if pos + 4 > len(raw):
            return b""
        (key_count,) = struct.unpack(">I", raw[pos:pos + 4])
        pos += 4
        if key_count < 1:
            return b""
        # 公钥部分始终是明文，因此带口令的私钥也能读出公钥。
        blob, _ = _read_string(raw, pos)
        return blob
    except (ValueError, struct.error):
        return b""


def public_key_line(private_key: str, comment: str = "") -> str:
    """把私钥对应的公钥整理成可直接粘贴到 GitHub 的单行文本。

    形如 ``ssh-ed25519 AAAAC3Nza... 备注``；无法推导时返回空串。
    """
    blob = public_blob(private_key)
    if not blob:
        return ""
    try:
        key_type, _ = _read_string(blob, 0)
    except ValueError:
        return ""
    try:
        type_text = key_type.decode("ascii")
    except UnicodeDecodeError:
        return ""
    encoded = base64.b64encode(blob).decode("ascii")
    suffix = f" {comment.strip()}" if comment and comment.strip() else ""
    return f"{type_text} {encoded}{suffix}"


def fingerprint(private_key: str) -> str:
    """计算公钥指纹，格式与 ``ssh-keygen -lf`` / GitHub 显示一致。

    注意必须对**公钥**块取摘要：直接用私钥文件内容哈希会得到完全不同的值，
    导致用户拿它跟 GitHub 上的指纹核对时永远对不上。
    """
    blob = public_blob(private_key)
    if not blob:
        return ""
    digest = hashlib.sha256(blob).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def key_type_of(private_key: str) -> str:
    """返回私钥的算法名（如 ``ssh-ed25519``），未知时为空串。"""
    blob = public_blob(private_key)
    if not blob:
        return ""
    try:
        key_type, _ = _read_string(blob, 0)
        return key_type.decode("ascii")
    except (ValueError, UnicodeDecodeError):
        return ""


def generate_keypair(
    *,
    key_type: str = "ed25519",
    comment: str = "autodeploy",
    timeout: int = 30,
) -> tuple[str, str, str]:
    """生成密钥对，返回 ``(私钥, 公钥单行, 指纹)``。

    私钥以 ``0600`` 落在临时目录，读取后立即整目录删除，不留在磁盘上。
    未安装 ``ssh-keygen`` 时抛出 :class:`RuntimeError`，由调用方转成中文提示。
    """
    binary = shutil.which("ssh-keygen")
    if not binary:
        raise RuntimeError("服务器未安装 ssh-keygen，无法自动生成密钥；请手工生成后粘贴私钥")

    algo = key_type if key_type in KEY_TYPES else "ed25519"
    tmp_dir = Path(tempfile.mkdtemp(prefix="autodeploy-sshkey-"))
    key_path = tmp_dir / "id_deploy"
    try:
        args = [binary, "-t", algo, "-f", str(key_path), "-N", "", "-C", comment or "autodeploy", "-q"]
        if algo == "rsa":
            args += ["-b", "4096"]
        result = run_command(args, timeout=timeout)
        if not result.ok:
            detail = (result.output or result.error or "").strip().splitlines()
            raise RuntimeError(f"生成密钥失败：{detail[-1] if detail else '未知错误'}")

        private_path = key_path
        public_path = tmp_dir / "id_deploy.pub"
        if not private_path.exists() or not public_path.exists():
            raise RuntimeError("生成密钥失败：ssh-keygen 未产出预期文件")

        private_text = private_path.read_text(encoding="utf-8")
        public_text = public_path.read_text(encoding="utf-8").strip()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return private_text, public_text, fingerprint(private_text)

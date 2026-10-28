#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pagent — 零依赖命令行 Agent，仅支持 OpenAI 兼容的 /chat/completions。

配置优先级：命令行参数 > 环境变量 > pagent.local.py > 文件顶部 DEFAULT_* 常量。
（pagent.local.py 在 .gitignore 里，专门放密钥；仓库里的 DEFAULT_* 必须保持空串）
环境变量：PAGENT_API_KEY、PAGENT_MODEL、PAGENT_BASE_URL、PAGENT_WORKDIR、PAGENT_ALLOW_OUTSIDE、
PAGENT_CONTEXT_TOKENS(262144)、PAGENT_MAX_OUTPUT_TOKENS(32768)、PAGENT_TIMEOUT(300)、
PAGENT_RETRIES(3)、PAGENT_RETRIES_429(比 PAGENT_RETRIES 多 5 次)。

用法：python pagent.py [--workdir DIR] [--yes] [--no-stream] [--no-tools]
     echo "问题" | python pagent.py    # 单次执行
"""

import argparse
import http.client
import json
import locale
import os
import random
import re
import shlex
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import suppress
from email.utils import parsedate_to_datetime

# ================================================================ 默认配置
# 这三行在仓库里必须保持空串！真实配置写进同目录的 pagent.local.py（已被 .gitignore 忽略），
# 否则密钥会被提交到公开仓库。详见 README「配置」一节。
DEFAULT_BASE_URL = ""  # 对应 PAGENT_BASE_URL
DEFAULT_API_KEY = ""  # 对应 PAGENT_API_KEY
DEFAULT_MODEL = ""  # 对应 PAGENT_MODEL
# ================================================================

VERSION = "0.0.1"
MAX_ROUNDS = 32          # 单轮工具往返上限
OUT_LIMIT = 60000        # 单条工具结果最大字符数
KEEP_TOOL_CHARS = 2000   # 上下文超预算时，工具结果压缩后保留的字符数上限
KEEP_TOOL_CHARS_MIN = 400  # 压缩下限，保证工具结果仍有可用信息
CHARS_PER_TOKEN = 3      # 1 token ≈ 3 字符（中英混合，偏保守）
CHUNK = 8192             # read_file 分片字节数（默认=上限）
QUIT_AT = 3              # 连按几次 Ctrl+C 才退出（QUIT_AT）

# 重试退避（指数 + 抖动）：1, 2, 4, 8, 16, 32, 60... 秒
RETRY_BASE = 1.0         # 首次退避基准秒数
RETRY_FACTOR = 2.0       # 指数退避倍数
RETRY_CAP = 60.0         # 单次退避上限（秒）
RETRY_JITTER = 0.4       # 抖动幅度 ±40%，避免固定节奏撞上限流窗口
RETRY_429_EXTRA = 5      # 429 在普通重试次数之上额外增加的尝试次数
RETRY_AFTER_MAX = 120.0  # 服务端 Retry-After 的最大采纳值（秒）

DEFAULT_SYSTEM = (
    "你是 pagent，一个极简的命令行编程 Agent。"
    "你有五个工具：shell(执行命令)、read_file(分片读文件)、edit_file(精确替换)、"
    "write_file(写文件/追加)、list_dir(列目录)。"
    "read_file 每次最多读 8KB，返回续读 offset；输出带行号前缀（如 42→ 代码），"
    "行号只为定位、不属于文件内容，抄进 old_string 时不要带行号。"
    "改已有文件优先用 edit_file：old_string 必须与原文逐字符一致且在文件中唯一，"
    "只替换需要改的那一小段，不要整文件重写；匹配不到或不唯一时，重新 read_file "
    "取更长的上下文再试。新建文件或整篇重写才用 write_file。"
    "大文件必须逐段读完，不要臆测没读到的部分。"
    "写长内容优先 write_file，不要用 shell 拼接。"
    "文件工具仅限当前工作目录内，越界路径会被拒绝，优先用相对路径。"
    "shell 在本机执行(Linux 走 sh -c，Windows 走 cmd /c)，命令风格按平台写。"
    "普通命令（查看、读取、搜索、编译、跑测试、git status 等）直接执行，不需要申请。"
    "只有高危命令才请求批准：删除或覆盖、改权限、改系统配置或服务、关机重启、格式化磁盘、"
    "下载后直接执行、改写 git 历史、写入工作目录之外。"
    "请求批准前先用一句话说明要做什么、有什么风险，不要先执行再解释。"
    "write_file 覆盖已有文件同样会请求批准，追加或新建文件直接执行。"
    "被拒绝就换更安全的方案。"
    "任务不明确时先向用户澄清。任务完成后停止，用两三句话总结做了什么。"
    "始终用中文回答，回答简洁。不要编造工具结果。"
)

_TTY = sys.stdout.isatty()
_AUTO_YES = False
_ROOT = {"path": os.getcwd(), "jail": True}
_CTRL_C = {"n": 0}
_USAGE = {"calls": 0, "in": 0, "out": 0, "est": 0}   # 累计 token 用量（est=估算次数）


def _env(name, default=None, kind=str):
    v = os.environ.get(name)
    if v in (None, ""):
        return default
    try:
        return kind(v)
    except ValueError:
        return default


def _die(msg):
    sys.stderr.write(msg + "\n")
    sys.exit(2)


def _local_cfg():
    """读同目录 pagent.local.py 里的 BASE_URL / API_KEY / MODEL（存在才读）。

    密钥放这个文件而不是直接写 DEFAULT_*，是因为它在 .gitignore 里，
    不会随 git add -A 被误提交到公开仓库。
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pagent.local.py")
    if not os.path.isfile(path):
        return {}
    ns = {}
    try:
        with open(path, encoding="utf-8") as f:
            exec(compile(f.read(), path, "exec"), ns)
    except Exception as e:
        sys.stderr.write("警告：读取 %s 失败（%s: %s），已忽略\n" % (path, type(e).__name__, e))
        return {}
    return {k: ns.get(k) for k in ("BASE_URL", "API_KEY", "MODEL")}


def _ctrl_c(cancelled):
    """记一次 Ctrl+C；返回 True 表示该退出了。"""
    _CTRL_C["n"] += 1
    left = QUIT_AT - _CTRL_C["n"]
    if left <= 0:
        print("\n[退出]")
        return True
    print("\n[%s再按 %d 次 Ctrl+C 退出]" % ("已中断，" if cancelled else "", left))
    return False


def _dim(s):
    return "\033[2m" + s + "\033[0m" if _TTY else s


def _clip(text, limit=OUT_LIMIT):
    return text if len(text) <= limit else text[:limit] + "\n...[已截断，共 %d 字符]" % len(text)


def _err(e):
    return "错误：%s: %s" % (type(e).__name__, e)


def _chars(messages):
    """请求体字符数，服务端不给 usage 时用来估算输入 token。"""
    return sum(len(json.dumps(m, ensure_ascii=False)) for m in messages)


def _usage(usage, in_chars, out_chars):
    """累计 token 用量。服务端回了 usage 就用真实值，否则按字符数估算。"""
    _USAGE["calls"] += 1
    p = c = None
    if isinstance(usage, dict):
        p, c = usage.get("prompt_tokens"), usage.get("completion_tokens")
    if isinstance(p, int) and isinstance(c, int):
        _USAGE["in"] += p
        _USAGE["out"] += c
        return
    _USAGE["est"] += 1
    _USAGE["in"] += in_chars // CHARS_PER_TOKEN
    _USAGE["out"] += out_chars // CHARS_PER_TOKEN


def _tok(n):
    return "%.1fk" % (n / 1000.0) if n >= 1000 else str(n)


def _usage_line():
    u = _USAGE
    if not u["calls"]:
        return "用量     : 本次会话还没有请求"
    return "用量     : %d 次请求，输入 %s / 输出 %s tokens%s" % (
        u["calls"], _tok(u["in"]), _tok(u["out"]),
        "（其中 %d 次为估算）" % u["est"] if u["est"] else "")


# ---------------------------------------------------------------- 路径沙箱

def _inside(path, root):
    root, real = os.path.realpath(root), os.path.realpath(path)
    try:   # commonpath 按路径组件比较，正确处理盘符根（C:\ 或 /）与大小写差异
        return os.path.normcase(os.path.commonpath([root, real])) == os.path.normcase(root)
    except ValueError:   # 不同盘符
        return False


def _resolve(path):
    p = os.path.abspath(os.path.expanduser(path or "."))
    if _ROOT["jail"] and not _inside(p, _ROOT["path"]):
        return None, "错误：路径越界，只允许在 %s 内操作（--allow-outside 可放行）" % _ROOT["path"]
    return p, None


# ---------------------------------------------------------------- 高危命令识别

# 一律要求确认的命令词（切段后的首个命令，含 Windows 别名）
RISKY_CMDS = set("""
    rm rmdir rd del erase shred unlink
    chmod chown chgrp sudo su doas runas takeown icacls
    reg regedit sc systemctl service launchctl netsh bcdedit
    shutdown reboot halt poweroff
    dd mkfs format diskpart fdisk parted
    kill killall pkill taskkill crontab schtasks
    remove-item remove-itemproperty clear-content stop-process stop-service
    restart-computer stop-computer set-executionpolicy invoke-expression iex
    format-volume clear-disk remove-partition
""".split())
STDIN_EXECUTORS = {"sh", "bash", "zsh", "dash", "python", "python3", "node", "perl", "ruby", "iex"}
SHELL_WRAPPERS = {"cmd", "powershell", "pwsh", "sh", "bash", "zsh", "dash", "env"}
SAFE_REDIRECTS = {"/dev/null", "/dev/zero", "/dev/stdout", "/dev/stderr", "/dev/tty",
                  "nul", "nul:", "$null", "con"}


def _risk_move(rest):
    """mv/move/ren：只有目标越界或已存在（会被覆盖）才算高危。"""
    args = [t for t in rest if not t.startswith("-")]
    if len(args) < 2:
        return ""
    dest = args[-1]
    p, err = _resolve(dest)
    if err:
        return "移动目标在工作目录之外（%s）" % dest
    if os.path.exists(p):
        return "目标已存在，会被覆盖（%s）" % dest
    return ""


def _risk_git(rest):
    sub, flags = (rest[0] if rest else ""), rest[1:]
    if sub == "push" and any(f in ("--force", "-f", "--mirror", "--force-with-lease")
                             for f in flags):
        return "强推会覆盖远端历史"
    if sub == "reset" and "--hard" in flags:
        return "git reset --hard 会丢弃未提交的改动"
    if sub == "clean" and any(f.startswith("-") and "f" in f for f in flags):
        return "git clean -f 会删除未跟踪文件"
    if sub in ("filter-branch", "filter-repo"):
        return "改写 git 历史"
    if sub == "rebase" and "-i" in flags:
        return "交互式 rebase 会改写历史"
    if sub == "restore":
        return "git restore 会丢弃工作区未提交的改动"
    if sub == "checkout" and any(f in (".", "--", "-f") for f in flags):
        return "会丢弃工作区未提交的改动"
    return ""


def _risk_redirect(cmd):
    """写入工作目录之外的重定向（>/>>）算高危。"""
    for m in re.finditer(r"(?<![0-9&])>>?\s*([^\s;&|<>()]+)", cmd):
        target = m.group(1).strip("\"'")
        if target.lower() in SAFE_REDIRECTS:
            continue
        _, err = _resolve(target)
        if err:
            return "重定向写入工作目录之外（%s）" % target
    return ""


def _risk_segment(seg, depth):
    try:
        tokens = shlex.split(seg, posix=(os.name != "nt"))
    except ValueError:
        tokens = seg.split()
    if not tokens:
        return ""
    head = os.path.basename(tokens[0].strip("\"'")).lower()
    if head.endswith(".exe"):
        head = head[:-4]
    rest = tokens[1:]

    if head in STDIN_EXECUTORS and not rest:
        return "把管道内容直接交给解释器执行"
    if head in SHELL_WRAPPERS:   # cmd /c、sh -c、powershell -Command 里的内容继续解析
        for i, t in enumerate(rest):
            if t.lower() in ("-c", "/c", "-command"):
                return _risk(" ".join(rest[i + 1:]).strip().strip("\"'"), depth + 1)
        return _risk(" ".join(rest), depth + 1) if rest else ""

    if head in RISKY_CMDS or head.startswith("mkfs"):
        return "高危命令 %s" % head
    if head in ("find", "xargs") and any(os.path.basename(t) in RISKY_CMDS or t == "-delete"
                                         for t in rest):
        return "通过 %s 执行删除" % head
    if head in ("mv", "move", "ren", "rename"):
        return _risk_move(rest)
    if head == "git":
        return _risk_git(rest)
    if head in ("pip", "pip3") and "uninstall" in rest:
        return "卸载 Python 包"
    if head in ("npm", "pnpm", "yarn") and any(a in ("-g", "--global") for a in rest):
        return "全局安装/卸载 node 包"
    return ""


def _risk(cmd, depth=0):
    """静态判断命令是否高危，返回原因；安全返回 ""。

    能力边界：只做静态识别，python -c / 编译执行等无法覆盖；
    宁可漏判也不误判（误判会让用户每次都被打断）。
    """
    if depth > 1:
        return ""
    for m in re.finditer(r"\$\(([^()]*)\)|`([^`]*)`", cmd):   # 命令替换里的内容也要看
        reason = _risk(m.group(1) or m.group(2) or "", depth + 1)
        if reason:
            return reason
    for seg in re.split(r"\|\||&&|;|\||\n", cmd):
        reason = _risk_segment(seg.strip(), depth) if seg.strip() else ""
        if reason:
            return reason
    return _risk_redirect(cmd)


# ---------------------------------------------------------------- 工具实现

def _confirm(question):
    if _AUTO_YES:
        print("  [自动批准]", question)
        return True
    if not sys.stdin.isatty():
        print("  [非交互环境，拒绝执行]", question)
        return False
    print("  " + question)
    try:
        return input("  允许执行? [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _decode(raw):
    """子进程输出解码：utf-8 → 系统首选编码 → gbk，避免中文 Windows 下乱码。"""
    seen = set()
    for enc in ("utf-8", locale.getpreferredencoding(False), "gbk", "mbcs"):
        if not enc or enc.lower() in seen:
            continue
        seen.add(enc.lower())
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def _kill_tree(proc):
    """连子进程一起杀掉，避免 shell=True 留下孤儿进程。"""
    with suppress(Exception):
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=10)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    with suppress(Exception):
        proc.kill()


def _run_cmd(cmd, timeout):
    """执行命令；返回 (returncode|None, stdout, stderr)，None 表示超时。"""
    proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True,
                            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        out, err = b"", b""
        with suppress(subprocess.TimeoutExpired):
            out, err = proc.communicate(timeout=5)
        return None, out, err
    except BaseException:      # Ctrl+C 等：先清理子进程树，避免留下孤儿
        _kill_tree(proc)
        raise
    return proc.returncode, out, err


def tool_shell(a):
    cmd = a.get("command", "")
    if not cmd.strip():
        return "错误：command 为空"
    reason = _risk(cmd)
    if reason and not _confirm("$ %s\n  高危：%s" % (cmd, reason)):
        return "用户拒绝了该命令，未执行。请换一种方式或询问用户。"
    try:
        timeout = min(max(float(a.get("timeout", 120)), 1.0), 3600.0)
    except (TypeError, ValueError):
        timeout = 120.0
    try:
        code, out, err = _run_cmd(cmd, timeout)
    except Exception as e:
        return _err(e)
    out, err = _decode(out), _decode(err)
    parts = ["错误：命令超时（%.0f 秒），已终止进程树" % timeout if code is None
             else "exit_code=%d" % code]
    if out.strip():
        parts.append("stdout:\n" + out.rstrip())
    if err.strip():
        parts.append("stderr:\n" + err.rstrip())
    return _clip("\n".join(parts))


def _utf8_prefix(raw, cut):
    """解码字节前缀，返回 (text, consumed)。cut=分片截断时，末尾若切在多字节字符
    中间则回退到字符起点；cut=已读到文件末尾时，坏字节只 replace 不丢弃。"""
    try:
        return raw.decode("utf-8"), len(raw)
    except UnicodeDecodeError as e:
        if cut and len(raw) - e.start <= 3:
            return raw[:e.start].decode("utf-8", "replace"), e.start
        return raw.decode("utf-8", "replace"), len(raw)


def _count_lines(path, upto):
    """数文件前 upto 字节里有几个换行，用于给分片内容标行号。"""
    n, left = 0, upto
    try:
        with open(path, "rb") as f:
            while left > 0:
                block = f.read(min(65536, left))
                if not block:
                    break
                n += block.count(b"\n")
                left -= len(block)
    except OSError:
        return 0
    return n


def _number(text, start):
    """给文本加行号前缀（42→ ...），末尾换行原样保留。"""
    tail = "\n" if text.endswith("\n") else ""
    lines = (text[:-1] if tail else text).split("\n")
    width = len(str(start + len(lines) - 1))
    return "\n".join("%*d→ %s" % (width, start + i, ln) for i, ln in enumerate(lines)) + tail


def tool_read(a):
    path, err = _resolve(a.get("path", ""))
    if err:
        return err
    try:
        total = os.path.getsize(path)
        offset = max(0, int(a.get("offset", 0)))
        n = min(CHUNK, max(1, int(a.get("max_bytes", CHUNK))))
    except (OSError, TypeError, ValueError) as e:
        return _err(e)
    if offset > total:
        return "错误：offset=%d 超出文件大小 %d" % (offset, total)
    if total == 0:
        return "（空文件，0 字节）"
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            raw = f.read(n + 1)
    except OSError as e:
        return _err(e)
    more, raw = len(raw) > n, raw[:n]
    if offset:   # offset 落在多字节字符中间：跳过残留续字节
        skip = 0
        while skip < min(3, len(raw)) and (raw[skip] & 0xC0) == 0x80:
            skip += 1
        raw, offset = raw[skip:], offset + skip
    text, consumed = _utf8_prefix(raw, more)
    end = offset + consumed
    if text:
        text = _number(text, _count_lines(path, offset) + 1)
    if more:
        text += "\n...[分片读取：字节 %d-%d / 共 %d，续读请传 offset=%d]" % (offset, end, total, end)
    elif offset:
        text += "\n...[分片读取：字节 %d-%d，已到文件末尾]" % (offset, end)
    return _clip(text)


def tool_write(a):
    path, err = _resolve(a.get("path", ""))
    if err:
        return err
    content = a.get("content", "")
    mode = "append" if a.get("mode") == "append" else "overwrite"
    if mode == "overwrite" and os.path.isfile(path):
        old = 0
        with suppress(OSError):
            old = os.path.getsize(path)
        if not _confirm("覆盖已有文件 %s（原 %d 字节 → 新 %d 字节）"
                        % (path, old, len(str(content).encode("utf-8")))):
            return "用户拒绝了覆盖写入，文件未修改。可改用 mode=append 或询问用户。"
    try:
        parent = os.path.dirname(path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        with open(path, "a" if mode == "append" else "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
    except (OSError, TypeError) as e:
        return _err(e)
    return "已写入 %d 字节到 %s（%s）" % (len(content.encode("utf-8")), path,
                                        "追加" if mode == "append" else "覆盖")


def tool_edit(a):
    """按 old_string → new_string 精确替换，要求 old_string 在文件中唯一。

    比 write_file 重写整篇便宜得多，也不会误伤文件其它部分；
    唯一性检查是安全底线：匹配到多处就报错让模型补上下文，绝不猜。
    """
    path, err = _resolve(a.get("path", ""))
    if err:
        return err
    old, new = a.get("old_string"), a.get("new_string")
    if not isinstance(old, str) or not old:
        return "错误：old_string 不能为空"
    if not isinstance(new, str):
        return "错误：new_string 必须是字符串（删除内容请传空字符串）"
    if old == new:
        return "错误：old_string 与 new_string 相同，无需修改"
    if not os.path.isfile(path):
        return "错误：文件不存在 %s（新建请用 write_file）" % path
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            text = f.read()
    except UnicodeDecodeError:
        return "错误：%s 不是 UTF-8 文本，无法按文本编辑" % path
    except OSError as e:
        return _err(e)
    hits = text.count(old)
    if hits == 0:
        return ("错误：未找到 old_string。必须与文件内容逐字符一致（含缩进、空行、标点），"
                "行号前缀不属于文件内容。请重新 read_file 核对后再试。")
    if hits > 1 and not a.get("replace_all"):
        return ("错误：old_string 在文件中出现 %d 次，不唯一。请把它扩展成更长的上下文使其唯一，"
                "或传 replace_all=true 全部替换。" % hits)
    text = text.replace(old, new) if a.get("replace_all") else text.replace(old, new, 1)
    try:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
    except OSError as e:
        return _err(e)
    for ln in old.splitlines()[:4]:
        print(_dim("  - " + ln[:160]))
    for ln in new.splitlines()[:4]:
        print(_dim("  + " + ln[:160]))
    return "已替换 %d 处，%s 现在共 %d 行" % (hits if a.get("replace_all") else 1, path,
                                             text.count("\n") + (0 if text.endswith("\n") else 1))


def tool_list(a):
    path, err = _resolve(a.get("path", "."))
    if err:
        return err
    try:
        with os.scandir(path) as it:
            entries = sorted(it, key=lambda e: (not e.is_dir(), e.name))
    except OSError as e:
        return _err(e)
    lines = []
    for e in entries:
        try:
            lines.append(("d  %s/" % e.name) if e.is_dir()
                         else "%-10d %s" % (e.stat().st_size, e.name))
        except OSError:
            lines.append("?  " + e.name)
    return _clip("\n".join(lines) or "（空目录）")


TOOL_IMPL = {"shell": tool_shell, "read_file": tool_read, "edit_file": tool_edit,
             "write_file": tool_write, "list_dir": tool_list}


def _fn(name, desc, props=None, required=()):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props or {}, "required": list(required)}}}


TOOLS = [
    _fn("shell", "执行 shell 命令，返回 stdout/stderr/退出码。普通命令直接执行，仅高危命令会请求用户批准。",
        {"command": {"type": "string"},
         "timeout": {"type": "number", "description": "秒，默认 120"}}, ["command"]),
    _fn("read_file", "分片读取文本文件，每次最多 8KB，输出带行号前缀（42→ 代码，行号不属于文件内容）。"
                     "文件没读完时返回续读 offset。",
        {"path": {"type": "string"},
         "offset": {"type": "integer", "description": "起始字节，默认 0"},
         "max_bytes": {"type": "integer", "description": "≤8192，默认 8192"}}, ["path"]),
    _fn("edit_file", "修改已有文件的首选方式：把 old_string 精确替换为 new_string。"
                     "old_string 必须与文件内容逐字符一致且在文件中唯一（不要带行号前缀），"
                     "否则报错不写入。只改局部内容，不要整文件重写。",
        {"path": {"type": "string"},
         "old_string": {"type": "string", "description": "被替换的原文，需唯一"},
         "new_string": {"type": "string", "description": "替换后的内容，可为空表示删除"},
         "replace_all": {"type": "boolean", "description": "为 true 时替换全部匹配（默认 false）"}},
        ["path", "old_string", "new_string"]),
    _fn("write_file", "写文本文件，父目录自动创建。覆盖已有文件前会请求用户批准，追加或新建不请求。",
        {"path": {"type": "string"}, "content": {"type": "string"},
         "mode": {"type": "string", "enum": ["overwrite", "append"]}}, ["path", "content"]),
    _fn("list_dir", "列出目录内容。", {"path": {"type": "string"}}),
]


# ---------------------------------------------------------------- HTTP

class ApiError(Exception):
    pass


HINTS = {
    400: "请求被拒绝（参数或字段不被该模型支持）",
    401: "鉴权失败，检查 PAGENT_API_KEY",
    403: "无权访问（权限、实名认证或区域受限）",
    404: "接口或模型不存在，检查 PAGENT_BASE_URL 与 PAGENT_MODEL",
    413: "请求体过大，上下文超出服务端上限，调小 PAGENT_CONTEXT_TOKENS",
    422: "参数校验失败",
    429: "触发限流或配额不足",
}
RETRY_CODES = (408, 409, 425, 429)


def _friendly(code, detail):
    detail = detail.strip()
    hint, low = HINTS.get(code, ""), detail.lower()
    if code == 400 and any(k in low for k in ("context_length", "maximum context length", "too long")):
        hint = "上下文超长，调小 PAGENT_CONTEXT_TOKENS 或 /reset"
    elif code == 429 and ("实名" in detail or "real-name" in low or "rpm" in low):
        hint += "（需实名认证才能提高 RPM）"
    msg = "HTTP %d" % code + (" — " + hint if hint else "")
    return msg + ("\n" + detail[:600] if detail else "")


def _retry_after(e):
    """解析 Retry-After（秒数或 HTTP-date），没有则返回 None。"""
    ra = (e.headers.get("Retry-After") if e.headers else None) or ""
    ra = str(ra).strip()
    if not ra:
        return None
    try:
        secs = (float(ra) if ra.replace(".", "", 1).isdigit()
                else parsedate_to_datetime(ra).timestamp() - time.time())
    except (TypeError, ValueError, OverflowError):
        return None
    return max(0.0, min(secs, RETRY_AFTER_MAX))


def _backoff(attempt, retry_after=None):
    """指数退避 + 随机抖动，避免所有请求按同一节奏撞限流窗口。

    第 n 次重试等待 RETRY_BASE * RETRY_FACTOR**n，再乘 1±RETRY_JITTER 的随机系数，
    上限 RETRY_CAP；服务端给了 Retry-After 就至少等那么久（可突破上限）。
    """
    delay = min(RETRY_CAP, RETRY_BASE * (RETRY_FACTOR ** attempt))
    delay *= 1.0 + random.uniform(-RETRY_JITTER, RETRY_JITTER)
    delay = min(max(delay, 0.2), RETRY_CAP)
    if retry_after is not None:
        delay = max(delay, retry_after)
    return delay


def _net_error(e):
    if isinstance(e, urllib.error.URLError):
        return "网络不可达或连接超时 — %s" % getattr(e, "reason", e)
    if isinstance(e, TimeoutError):
        return "读取超时 — %s（可调大 PAGENT_TIMEOUT）" % e
    return "连接中断 — %s: %s" % (type(e).__name__, e)


def _post(payload, cfg):
    """返回已打开的 response。按错误码决定是否重试，429 用更高的重试上限。"""
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    limit, attempt, last = max(1, cfg["retries"]), 0, "请求失败"
    while True:
        attempt += 1
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + cfg["api_key"])
        req.add_header("Accept", "text/event-stream" if payload["stream"] else "application/json")
        req.add_header("User-Agent", "pagent/" + VERSION)
        try:
            return urllib.request.urlopen(req, timeout=cfg["timeout"])
        except urllib.error.HTTPError as e:
            detail = ""
            with suppress(Exception):
                detail = e.read().decode("utf-8", "replace")
            last = _friendly(e.code, detail)
            if e.code == 429:   # 限流通常等一会儿就好，给更多次机会
                limit = max(limit, cfg["retries_429"])
            if e.code not in RETRY_CODES and e.code < 500:
                raise ApiError(last)
            wait = _backoff(attempt - 1, _retry_after(e))
        except ValueError as e:
            raise ApiError("请求构造失败 — %s" % e)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = _net_error(e)
            wait = _backoff(attempt - 1)
        if attempt >= limit:
            raise ApiError(last)
        sys.stderr.write("[第 %d 次失败，%.1f 秒后重试（上限 %d 次）] %s\n"
                         % (attempt, wait, limit, last.split("\n")[0]))
        sys.stderr.flush()
        time.sleep(wait)


def _payload(cfg, messages, stream):
    p = {"model": cfg["model"], "messages": messages,
         "max_tokens": cfg["max_output"], "stream": stream}
    if cfg["tools"]:
        p.update(tools=TOOLS, tool_choice="auto")
    return p


def chat(messages, cfg):
    try:
        with _post(_payload(cfg, messages, False), cfg) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except (OSError, http.client.HTTPException) as e:
        raise ApiError("响应读取中断 — %s: %s" % (type(e).__name__, e))
    try:
        data = json.loads(raw)
    except ValueError:
        raise ApiError("响应不是合法 JSON：" + raw[:300])
    if data.get("error") and not data.get("choices"):
        raise ApiError(str(data["error"])[:600])
    if not data.get("choices"):
        raise ApiError("响应无 choices：" + raw[:300])
    msg = (data["choices"][0] or {}).get("message") or {}
    _usage(data.get("usage"), _chars(messages),
           len(_text(msg)) + sum(len((c.get("function") or {}).get("arguments") or "")
                                 for c in msg.get("tool_calls") or []))
    return data


def chat_stream(messages, cfg):
    """流式请求，边收边打印（思考段暗色）。返回 (正文, 工具调用)。"""
    text, calls, junk = [], {}, []
    seen = {"think": False, "text": False}
    usage = None
    try:
        with _post(_payload(cfg, messages, True), cfg) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    if line and not line.startswith(":"):   # ":" 是 SSE 注释/心跳
                        junk.append(line)
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    obj = json.loads(chunk)
                except ValueError:
                    continue
                if obj.get("error") and not obj.get("choices"):
                    raise ApiError(str(obj["error"])[:600])
                if isinstance(obj.get("usage"), dict):
                    usage = obj["usage"]   # 部分服务端在最后一个分片带用量
                for ch in obj.get("choices") or []:
                    d = ch.get("delta") or {}
                    pieces = (
                        ("think", d.get("reasoning_content") or d.get("reasoning")
                         or d.get("thinking") or ""),
                        ("text", d.get("content") or ""),
                    )
                    for kind, piece in pieces:
                        if not (isinstance(piece, str) and piece):
                            continue
                        if not seen[kind]:
                            seen[kind] = True
                            if kind == "think":
                                sys.stdout.write(_dim("\n· 思考: "))
                            elif seen["think"]:
                                sys.stdout.write(("\033[0m" if _TTY else "") + "\n\n")
                        sys.stdout.write(_dim(piece) if kind == "think" else piece)
                        sys.stdout.flush()
                        if kind == "text":
                            text.append(piece)
                    for tc in d.get("tool_calls") or []:
                        slot = calls.setdefault(tc.get("index", 0), {
                            "id": "", "type": "function",
                            "function": {"name": "", "arguments": ""}})
                        slot["id"] = tc.get("id") or slot["id"]
                        f = tc.get("function") or {}
                        name = f.get("name") or ""
                        if name and name != slot["function"]["name"] \
                                and not slot["function"]["name"].endswith(name):
                            slot["function"]["name"] += name   # 分片拼接，重发全名时不重复
                        slot["function"]["arguments"] += f.get("arguments") or ""
    except (OSError, http.client.HTTPException) as e:
        raise ApiError("流式读取中断 — %s: %s（已输出部分内容）" % (type(e).__name__, e))
    if seen["think"] or seen["text"]:
        sys.stdout.write(("\033[0m" if _TTY else "") + "\n\n")
        sys.stdout.flush()
    body = "".join(text)
    if not body.strip() and not calls and junk:
        raise ApiError("流式响应格式异常（服务端未按 SSE 返回）：" + "".join(junk)[:400])
    calls = _norm_calls([calls[k] for k in sorted(calls)])
    _usage(usage, _chars(messages),
           len(body) + sum(len(c["function"]["arguments"]) for c in calls))
    return body, calls


# ---------------------------------------------------------------- 上下文与轮次

def trim(messages, cfg):
    """把消息总量压到预算内，始终保证 assistant/tool 配对完整。

    三级策略：
      1) 整轮丢弃最旧的「user + 后续往返」；
      2) 单轮内没有更早的 user 可丢时，按超额比例压缩所有工具结果；
      3) 仍超预算则丢弃最旧的完整 assistant(tool_calls)+tool 块，保留最后一块。
    """
    budget = max(int((cfg["context"] - cfg["max_output"] - 512) * CHARS_PER_TOKEN), 4000)
    size = lambda ms: sum(len(json.dumps(m, ensure_ascii=False)) for m in ms)

    def find(start, pred):   # 从 start 起第一条满足 pred 的消息下标（可能为 len）
        i = start
        while i < len(messages) and not pred(messages[i]):
            i += 1
        return i

    used, dropped, blocks = size(messages), 0, 0

    # 阶段 1：整轮裁剪
    while used > budget and len(messages) > 2:
        j = find(2, lambda m: m["role"] == "user")
        if j >= len(messages):
            break
        used -= size(messages[1:j])
        dropped += j - 1
        del messages[1:j]
    if dropped:
        print("[已裁剪 %d 条早期消息以适配上下文预算]" % dropped)

    # 阶段 2：压缩工具结果
    tools = [m for m in messages
             if m.get("role") == "tool" and isinstance(m.get("content"), str)]
    squeezed = 0
    if used > budget and tools:
        total = sum(len(m["content"]) for m in tools)
        overhead = 80 * len(tools)   # 消息外壳（role/tool_call_id/JSON 结构）的固定开销
        keep = min(KEEP_TOOL_CHARS, max(KEEP_TOOL_CHARS_MIN,
                                        int((total - (used - budget) - overhead) / len(tools))))
        for m in tools:
            c = m["content"]
            if len(c) > keep:
                m["content"] = c[:keep] + "\n...[已压缩 %d 字符以适配上下文预算]" % (len(c) - keep)
                squeezed += 1
        used = size(messages)
    if squeezed:
        print("[已压缩 %d 条工具结果以适配上下文预算]" % squeezed)

    # 阶段 3：丢弃最旧的完整工具往返块（保留最后一块）
    while used > budget:
        i = find(2, lambda m: m.get("role") == "assistant" and m.get("tool_calls"))
        j = find(i + 1, lambda m: m.get("role") != "tool")
        if i >= len(messages) or j >= len(messages):
            break
        used -= size(messages[i:j])
        del messages[i:j]
        blocks += 1
    if blocks:
        print("[已丢弃 %d 个早期工具往返块以适配上下文预算]" % blocks)


def _text(msg):
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(p.get("text", "") for p in c if isinstance(p, dict))
    return ""


def _run_tool(call):
    fn = call.get("function") or {}
    name, raw = fn.get("name", ""), fn.get("arguments") or "{}"
    try:
        args = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (ValueError, TypeError):
        return "错误：arguments 不是合法 JSON: " + str(raw)[:200]
    impl = TOOL_IMPL.get(name)
    if not impl:
        return "错误：未知工具 " + name
    print("→ %s %s" % (name, json.dumps(args, ensure_ascii=False)[:200]))
    try:
        return _clip(str(impl(args)))
    except Exception as e:
        return _err(e)


def _norm_calls(calls):
    """归一化 tool_calls：只保留 id/type/function，避免把服务端私有字段回传导致校验失败。"""
    out = []
    for c in calls or []:
        fn = c.get("function") or {}
        out.append({"id": c.get("id", ""), "type": "function",
                    "function": {"name": fn.get("name", "") or "",
                                 "arguments": fn.get("arguments") or "{}"}})
    return out


def run_turn(messages, cfg):
    for _ in range(MAX_ROUNDS):
        trim(messages, cfg)
        if cfg["stream"]:
            content, calls = chat_stream(messages, cfg)
        else:
            msg = (chat(messages, cfg).get("choices") or [{}])[0].get("message") or {}
            content, calls = _text(msg), _norm_calls(msg.get("tool_calls"))
            if content.strip():
                print("\n" + content.strip() + "\n")
        entry = {"role": "assistant", "content": content or ""}
        if calls:
            entry["tool_calls"] = calls
        messages.append(entry)
        if not calls:
            return
        for call in calls:
            messages.append({"role": "tool", "tool_call_id": call.get("id", ""),
                             "content": _run_tool(call)})
    print("[达到工具调用轮数上限 %d，已停止]" % MAX_ROUNDS)


def _rollback(messages):
    """失败或中断后回滚到最后一个 user 消息，避免残留悬空 tool_calls 让下次请求 400。"""
    while len(messages) > 1 and messages[-1].get("role") != "user":
        messages.pop()


# ---------------------------------------------------------------- 入口

def load_config(args):
    """优先级：命令行参数 > 环境变量 > pagent.local.py > 文件顶部 DEFAULT_* 常量。"""
    local = _local_cfg()
    key_env, model_env = _env("PAGENT_API_KEY"), _env("PAGENT_MODEL")
    base_env = _env("PAGENT_BASE_URL")
    key = key_env or local.get("API_KEY") or DEFAULT_API_KEY
    model = args.model or model_env or local.get("MODEL") or DEFAULT_MODEL
    base_url = args.base_url or base_env or local.get("BASE_URL") or DEFAULT_BASE_URL
    key, model, base_url = key.strip(), model.strip(), base_url.strip()
    if not key or not model:
        msg = ("缺少 API Key / 模型名。四种配置方式任选其一：\n"
               "  1. 本地文件: 在同目录建 pagent.local.py，写 API_KEY = \"...\" / MODEL = \"...\"（推荐）\n"
               "  2. 环境变量: PAGENT_API_KEY / PAGENT_MODEL\n"
               "  3. 命令行  : --model 模型名（Key 只能走方式 1/2/4）\n"
               "  4. 改代码  : pagent.py 顶部的 DEFAULT_API_KEY / DEFAULT_MODEL"
               "（注意别提交到仓库）")
        old = [k for k in ("OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_BASE_URL") if os.environ.get(k)]
        if old:
            msg += "\n  提示：检测到旧变量 %s，已不再读取，请改用 PAGENT_* 前缀" % "、".join(old)
        _die(msg)
    if any(ord(c) > 127 for c in key):
        _die("API Key 含非 ASCII 字符（复制时多半带进了中文或全角符号），请检查")
    if not base_url:
        _die("未设置接口地址。请在 pagent.local.py 里写 BASE_URL，或设置 PAGENT_BASE_URL，"
             "例如 https://api.openai.com/v1")
    if not base_url.startswith(("http://", "https://")):
        _die("base_url 必须以 http:// 或 https:// 开头: %s" % base_url)
    workdir = os.path.abspath(args.workdir or _env("PAGENT_WORKDIR", os.getcwd()))
    if not os.path.isdir(workdir):
        _die("工作目录不存在: %s" % workdir)
    ctx = args.context if args.context is not None else _env("PAGENT_CONTEXT_TOKENS", 262144, int)
    max_out = (args.max_output if args.max_output is not None
               else _env("PAGENT_MAX_OUTPUT_TOKENS", 32768, int))
    if ctx <= 0 or max_out <= 0:
        _die("上下文与最大输出必须是正整数")
    if max_out >= ctx:
        sys.stderr.write("警告：最大输出(%d) >= 上下文(%d)，上下文预算将被压缩到最小档\n"
                         % (max_out, ctx))
    allow = args.allow_outside or \
        _env("PAGENT_ALLOW_OUTSIDE", "").strip().lower() in ("1", "true", "yes", "on")
    retries = max(1, _env("PAGENT_RETRIES", 3, int))
    retries_429 = max(retries, _env("PAGENT_RETRIES_429", retries + RETRY_429_EXTRA, int))
    return {
        "workdir": workdir,
        "jail": not allow,
        "base_url": base_url,
        "api_key": key,
        "model": model,
        "context": ctx,
        "max_output": max_out,
        "timeout": float(_env("PAGENT_TIMEOUT", 300, int)),
        "retries": retries,
        "retries_429": retries_429,
        "stream": args.stream,
        "tools": not args.no_tools,
        # 仅用于 status 提示：每项配置最终来自哪一层
        "sources": {
            "key": "env" if key_env else ("local" if local.get("API_KEY") else "default"),
            "model": "cli" if args.model else (
                "env" if model_env else ("local" if local.get("MODEL") else "default")),
            "base_url": "cli" if args.base_url else (
                "env" if base_env else ("local" if local.get("BASE_URL") else "default")),
        },
    }


def status(cfg):
    key = cfg["api_key"]
    masked = key[:4] + "…" + key[-4:] if len(key) > 12 else "****"
    src = cfg["sources"]
    mark = lambda k: {"cli": "（命令行）", "env": "", "local": "（本地文件）",
                      "default": "（代码默认值）"}[src[k]]
    return "\n".join([
        "pagent %s" % VERSION,
        "工作目录 : %s（%s）" % (cfg["workdir"], "沙箱开启" if cfg["jail"] else "无限制"),
        "Provider : %s%s" % (cfg["base_url"], mark("base_url")),
        "模型     : %s%s" % (cfg["model"], mark("model")),
        "鉴权     : Bearer %s%s" % (masked, mark("key")),
        "上下文   : %d tokens / 最大输出 %d" % (cfg["context"], cfg["max_output"]),
        "请求     : 超时 %.0fs，最多尝试 %d 次（429 最多 %d 次），%s" % (
            cfg["timeout"], cfg["retries"], cfg["retries_429"],
            "流式" if cfg["stream"] else "非流式"),
        "工具     : %s" % ("、".join(t["function"]["name"] for t in TOOLS)
                          if cfg["tools"] else "已禁用"),
        "确认策略 : %s" % ("全部自动批准（--yes）" if _AUTO_YES
                          else "仅高危命令需批准，普通命令直接执行"),
        _usage_line(),
    ])


def main(argv=None):
    p = argparse.ArgumentParser(description="零依赖 CLI Agent（OpenAI 兼容协议）")
    p.add_argument("--base-url", help="覆盖 PAGENT_BASE_URL / DEFAULT_BASE_URL")
    p.add_argument("--model", help="覆盖 PAGENT_MODEL / DEFAULT_MODEL")
    p.add_argument("--context", type=int, help="上下文 token 预算")
    p.add_argument("--max-output", type=int, help="单次最大输出 token")
    p.add_argument("--system", help="系统提示词")
    p.add_argument("--workdir", help="工作目录（文件工具沙箱根，默认当前目录）")
    p.add_argument("--allow-outside", action="store_true", help="放开工作目录限制")
    p.add_argument("--yes", "-y", action="store_true", help="自动批准 shell 命令（危险）")
    p.add_argument("--no-tools", action="store_true", help="禁用 function call")
    p.add_argument("--stream", dest="stream", action="store_true", help="强制流式输出")
    p.add_argument("--no-stream", dest="stream", action="store_false", help="关闭流式输出")
    p.set_defaults(stream=None)
    p.add_argument("--version", action="version", version="pagent " + VERSION)
    args = p.parse_args(argv)

    global _AUTO_YES
    _AUTO_YES = args.yes
    for s in (sys.stdout, sys.stderr):
        with suppress(Exception):
            s.reconfigure(encoding="utf-8", errors="replace")

    interactive = sys.stdin.isatty()
    if args.stream is None:
        args.stream = interactive and _TTY

    cfg = load_config(args)
    _ROOT.update(path=cfg["workdir"], jail=cfg["jail"])
    try:
        os.chdir(cfg["workdir"])
    except OSError as e:
        sys.stderr.write("无法进入 %s: %s\n" % (cfg["workdir"], e))
        return 2

    notice = args.system or DEFAULT_SYSTEM
    messages = [{"role": "system", "content": notice}]

    def submit(line):
        messages.append({"role": "user", "content": line})
        try:
            run_turn(messages, cfg)
        except ApiError as e:
            sys.stderr.write("[API 错误] %s\n" % e)
            _rollback(messages)
            return 1
        except (KeyboardInterrupt, BrokenPipeError):
            _rollback(messages)
            if not interactive:
                print("\n[已中断]")
                return -1
            return -1 if _ctrl_c(True) else 0
        return 0

    if not interactive:
        sys.stderr.write(status(cfg) + "\n\n")
        line = sys.stdin.read().strip()
        if not line:
            sys.stderr.write("stdin 为空\n")
            return 2
        rc = submit(line)
        return 0 if rc < 0 else rc

    print(status(cfg))
    print("\n命令: /status 状态  /exit 退出  /reset 清空历史  /tools 工具"
          "（Ctrl+C 连按 %d 次也可退出）\n" % QUIT_AT)
    while True:
        try:
            line = input("> ").strip()
        except EOFError:
            print()
            return 0
        except KeyboardInterrupt:
            if _ctrl_c(False):
                return 0
            continue
        _CTRL_C["n"] = 0   # 任何一次成功输入都重新计数
        if not line:
            continue
        if line.startswith("/"):
            if line in ("/exit", "/quit", "/q"):
                return 0
            elif line == "/reset":
                messages = [{"role": "system", "content": notice}]
                print("[历史已清空]\n")
            elif line == "/status":
                print(status(cfg) + "\n")
            elif line == "/tools":
                print(("\n".join("- %s: %s" % (t["function"]["name"], t["function"]["description"])
                                 for t in TOOLS) + "\n") if cfg["tools"] else "工具已禁用\n")
            else:
                print("可用命令: /status /reset /tools /exit\n")
            continue
        if submit(line) < 0:
            return 0


if __name__ == "__main__":
    sys.exit(main())

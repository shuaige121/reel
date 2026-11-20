#!/usr/bin/env python3
"""PreToolUse(Bash) hook：只守 `reel register` 登记过的交付目录，成片只能经 `reel deliver` 进去。

- 登记表为空 → 什么都不管。
- 逐个片段（; && || | 换行）判断：cp/mv/rsync/ditto/install/ln 的目标、ffmpeg/melt 的输出、重定向/tee 的目标、
  rm/touch/mkdir 等的参数落在交付目录里 → 拒绝。`reel deliver` 放行；同一条命令里 deliver 之后再 cp 坏片照样拒绝。
- 从交付目录读（ls、shasum、ffprobe、cp 出来）放行；素材复制到项目目录放行。
- 解析不了的写法（目标含变量、解释器/脚本、未知命令）只在命令里明文出现交付目录时拒绝，提示改走 reel deliver；
  完全看不到交付目录的 Python/远程写入拦不住，由 Stop hook（deliver_gate.py）拦「没有回执的已完成」。
- bypass（跳过权限确认）模式下照样生效；守卫自身出错时静默放行（不阻塞所有 Bash）。
"""
import json, os, re, shlex, sys

STATE = os.environ.get("REEL_STATE_DIR") or os.path.expanduser("~/.local/state/reel")
COPY_LIKE = {"cp", "rsync", "ditto", "install", "scp", "gcp", "ln"}
TOUCH_ANY = {"rm", "rmdir", "trash", "touch", "mkdir", "truncate", "chmod", "chown", "chflags", "unlink", "srm",
             "shred", "setfile", "sips", "exiftool"}
READONLY = {"ls", "stat", "cat", "head", "tail", "less", "more", "shasum", "sha256sum", "md5", "md5sum", "ffprobe",
            "mdls", "du", "df", "file", "wc", "grep", "rg", "diff", "cmp", "open", "echo", "printf", "test", "[",
            "realpath", "readlink", "basename", "dirname", "mediainfo", "afplay", "qlmanage", "pwd", "true", "false",
            "sleep", "which", "type", "date", "sort", "uniq", "jq", "tree", "mdfind", "xxd", "hexdump", "strings",
            ":", "return", "exit", "set", "unset", "local", "shift", "wait", "trap", "read", "fi", "done", "esac",
            "[[", "]]", "break", "continue"}
INTERP = {"python", "python3", "node", "ruby", "perl", "sh", "bash", "zsh", "osascript", "swift", "ssh", "xargs",
          "eval", "exec", "source", ".", "env", "nohup", "parallel", "watch", "timeout", "caffeinate", "sudo"}
WRAPPERS = {"command", "nice", "time", "builtin", "noglob", "then", "do", "else", "elif", "if", "while", "until", "!",
            "export"}
VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?:[:]?[-=?+][^}]*)?\}|\$([A-Za-z_][A-Za-z0-9_]*)")
ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)
REDIR = {">", ">>", ">|", "&>", "&>>", ">&", "<>"}
SEPS = {";", "&&", "||", "|", "&", "|&", "\n", "(", ")", "{", "}", "()", ";;"}
REEL_WRITE_FLAGS = {"--out", "--png", "--sheet", "--report", "--check-png"}
SAFE_REEL = {"deliver", "register", "receipt"}


def load_tasks():
    try:
        with open(os.path.join(STATE, "registry.json"), encoding="utf-8") as fh:
            tasks = json.load(fh).get("tasks", [])
    except (OSError, ValueError, AttributeError):
        return []
    return [t for t in tasks if isinstance(t, dict) and t.get("delivery_root")]


def load_roots():
    return list(dict.fromkeys(os.path.realpath(t["delivery_root"]) for t in load_tasks()))


def aliases(roots):
    """命令里可能出现的写法：realpath、登记时的写法、去掉 /private 前缀、~ 开头。用于「命令里明文提到交付目录」。"""
    home = os.path.expanduser("~")
    out = set()
    for t in load_tasks():
        for p in (t.get("delivery_root"), t.get("delivery_root_as_written")):
            if p:
                out.add(p)
    out.update(roots)
    for p in list(out):
        if p.startswith("/private/"):
            out.add(p[len("/private"):])
        if p.startswith(home + os.sep):
            out.add("~" + p[len(home):])
    return {_norm(p) for p in out}


def _norm(p):
    return p.lower() if sys.platform == "darwin" else p


def tokens(cmd):
    lex = shlex.shlex(cmd, posix=True, punctuation_chars="();<>|&\n")
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    return list(lex)


def segments(toks):
    seg = []
    for t in toks:
        if t in SEPS:
            if seg:
                yield seg
            seg = []
        else:
            seg.append(t)
    if seg:
        yield seg


class Ctx:
    def __init__(self, roots, cwd, raw):
        self.roots = roots
        self.cwd = cwd
        self.alias = aliases(roots)
        self.raw_mentions = False           # 每个片段在 check_segment 里重算
        self.vars = {"HOME": os.path.expanduser("~")}

    def assign(self, word):
        m = ASSIGN.match(word)
        if m:
            self.vars[m.group(1)] = self.expand(m.group(2)) or ""

    def expand(self, word):
        """展开本命令里赋过值的变量和 $HOME；命令替换 → None；环境里才有的变量按空串处理（Bash 工具每次是新 shell）。"""
        if "`" in word or "$(" in word:
            return None
        for m in VAR.finditer(word):
            name = m.group(1) or m.group(2)
            if name not in self.vars:
                d = re.match(r"\$\{[A-Za-z_][A-Za-z0-9_]*:?[-=]([^}]*)\}", m.group(0))
                if d:
                    self.vars[name] = self.expand(d.group(1)) or ""
        return VAR.sub(lambda m: self.vars.get(m.group(1) or m.group(2), ""), word)

    def resolve(self, word):
        """→ 绝对路径；含命令替换或相对路径而 cwd 未知 → None。"""
        word = self.expand(word)
        if word is None:
            return None
        w = os.path.expanduser(word)
        if not os.path.isabs(w):
            if self.cwd is None:
                return None
            w = os.path.join(self.cwd, w)
        return os.path.realpath(w)

    def inside(self, word):
        """True = 在交付目录里；False = 不在；None = 看不出来。"""
        p = self.resolve(word)
        if p is None:
            return None
        n = _norm(p)
        return any(n == _norm(r) or n.startswith(_norm(r) + os.sep) for r in self.roots)


def _positional(args):
    out, skip = [], False
    for i, a in enumerate(args):
        if skip:
            skip = False
            continue
        if a in ("-t", "--target-directory", "-e", "--rsh", "--exclude", "--include", "-S", "--suffix",
                 "--backup-dir", "--filter", "-f", "--files-from"):
            skip = True
            continue
        if a.startswith("-") and a != "-":
            continue
        out.append(a)
    return out


def _target_dir_flag(args):
    for i, a in enumerate(args):
        if a in ("-t", "--target-directory") and i + 1 < len(args):
            return args[i + 1]
        if a.startswith("--target-directory="):
            return a.split("=", 1)[1]
    return None


def check_segment(seg, ctx):
    """返回拒绝理由或 None。「提到交付目录」只看本片段（含 heredoc 正文切出来的行）。"""
    low = _norm(" ".join(seg))
    ctx.raw_mentions = any(a in low for a in ctx.alias)
    redir_targets, argv, i = [], [], 0
    while i < len(seg):
        t = seg[i]
        if t in REDIR or t in ("<", "<<", "<<<"):
            if t in REDIR and argv and argv[-1].isdigit():
                argv.pop()
            tgt = seg[i + 1] if i + 1 < len(seg) else ""
            if t in REDIR and not (t == ">&" and tgt.isdigit()):
                redir_targets.append(tgt)
            i += 2
            continue
        argv.append(t)
        i += 1
    for tg in redir_targets:
        hit = ctx.inside(tg)
        if hit or (hit is None and ctx.raw_mentions):
            return f"redirect writes to {tg}"
    while argv and (ASSIGN.match(argv[0]) or argv[0] in WRAPPERS):
        if argv[0] in ("command", "type") and len(argv) > 1 and argv[1] in ("-v", "-V"):
            return None                     # command -v 只是查找
        if ASSIGN.match(argv[0]):
            ctx.assign(argv[0])
        argv.pop(0)
    if not argv:
        return None
    if argv[0] == ":":                      # : "${X:=默认值}" 也是赋值
        for a in argv[1:]:
            ctx.expand(a)
        return None
    cmd, args = os.path.basename(argv[0]), argv[1:]
    if cmd == "cd":
        d = args[0] if args else "~"
        ctx.cwd = ctx.resolve(d) if d != "-" else None
        return None
    if cmd in ("python", "python3") and args and os.path.basename(args[0]) == "reel.py":
        cmd, args = "reel", args[1:]
    if cmd == "reel":
        sub = args[0] if args else ""
        if sub in SAFE_REEL:
            return None
        for k, a in enumerate(args[:-1]):
            if a in REEL_WRITE_FLAGS:
                hit = ctx.inside(args[k + 1])
                if hit or (hit is None and ctx.raw_mentions):
                    return f"reel {sub} {a} writes into the delivery folder (only reel deliver may)"
        return None
    if cmd in READONLY:
        return None
    if cmd == "find":
        if any(a in ("-delete", "-exec", "-execdir", "-ok", "-okdir") for a in args) and \
                any(ctx.inside(a) for a in args):
            return "find -delete/-exec acts on the delivery folder"
        return None
    if cmd == "xattr" and not any(a in ("-w", "-d", "-c", "-r") for a in args):
        return None
    if cmd == "rsync" and any(a == "--dry-run" or (re.match(r"^-[A-Za-z]+$", a) and "n" in a) for a in args):
        return None                         # rsync -n / --dry-run 只预览（重定向已在上面单独查过）
    if cmd == "tar":
        mode = next((a.lstrip("-") for a in args if a and not a.startswith("--")), "")
        if ("t" in mode or "--list" in args) and not set(mode) & set("xcru") and \
                not any(a in ("--extract", "--create", "--append", "--update", "--get") for a in args):
            return None                     # tar -t 只列出归档内容
    if cmd in COPY_LIKE or cmd == "mv":
        pos = _positional(args)
        dest = _target_dir_flag(args) or (pos[-1] if pos else None)
        checks = [dest] + (pos[:-1] if cmd == "mv" else [])
        for w in [x for x in checks if x]:
            hit = ctx.inside(w)
            if hit or (hit is None and ctx.raw_mentions):
                return f"{cmd} target {w}" if w == dest else f"mv moves {w} out of the delivery folder"
        return None
    if cmd in ("ffmpeg", "ffmpeg7", "avconv"):
        for k, a in enumerate(args):
            if k and args[k - 1] == "-i":
                continue
            if a.startswith("-"):
                continue
            hit = ctx.inside(a)
            if hit or (hit is None and ctx.raw_mentions):
                return f"ffmpeg output {a}"
        return None
    if cmd == "tee":
        for a in _positional(args):
            hit = ctx.inside(a)
            if hit or (hit is None and ctx.raw_mentions):
                return f"tee writes to {a}"
        return None
    if cmd in TOUCH_ANY or cmd in ("dd", "curl", "wget", "tar", "unzip", "zip", "melt"):
        for a in args:
            for piece in re.split(r"[=:]", a) if cmd in ("dd", "melt") else [a]:
                if piece and ctx.inside(piece):
                    return f"{cmd} acts on the delivery folder ({a})"
        if ctx.raw_mentions and any(("$(" in a or "`" in a) for a in args):
            return f"{cmd} has a command substitution and the command mentions the delivery folder; cannot confirm it is safe"
        return None
    # 解释器、脚本、未知命令：读不出它会写哪里。命令里明文出现交付目录就拒绝。
    if ctx.raw_mentions or any(ctx.inside(a) for a in args):
        kind = "interpreter/script" if cmd in INTERP else "unrecognised command"
        return f"{kind} {cmd}: the command mentions the delivery folder; cannot confirm it does not write there"
    return None


def decide(command, cwd, roots):
    if not roots or not command.strip():
        return None
    ctx = Ctx(roots, cwd, command)
    try:
        toks = tokens(command)
    except ValueError:
        return "command cannot be parsed (unbalanced quotes?)" if any(a in _norm(command) for a in ctx.alias) else None
    for seg in segments(toks):
        why = check_segment(seg, ctx)
        if why:
            return why
    return None


def main():
    try:
        data = json.load(sys.stdin)
        if data.get("tool_name") not in (None, "Bash"):
            return
        command = (data.get("tool_input") or {}).get("command") or ""
        roots = load_roots()
        why = decide(command, data.get("cwd") or os.getcwd(), roots)
    except Exception:  # noqa: BLE001  守卫自身出错不能卡住所有 Bash
        return
    if why:
        msg = (f"[reel guard] denied: {why}. Registered delivery folders ({'; '.join(roots)}) can only be written by"
               " `reel deliver` (it re-checks the file, verifies the hash and writes a receipt). Copy source material into"
               " the project folder instead; reading the delivery folder (ls / shasum / ffprobe / copying out) is allowed."
               " If the delivery folder really must change (for example the user asks to tidy old versions), ask the"
               " user first, then run `reel register <brief> --remove` to unregister it.")
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                 "permissionDecisionReason": msg}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
    sys.exit(0)

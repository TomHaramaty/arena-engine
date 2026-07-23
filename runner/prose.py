"""Surgical editor for principles.md / hypotheses.md — append-only changelogs,
in-place status/evidence updates, new sections. Never rewrites whole files."""
import re


class SectionFile:
    def __init__(self, path):
        self.path = path
        self.lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def ids(self):
        return re.findall(r"^##\s+([PH]\d+)\s*·", "\n".join(self.lines), re.M)

    def next_id(self, prefix):
        nums = [int(i[1:]) for i in self.ids() if i.startswith(prefix)]
        return f"{prefix}{max(nums, default=0) + 1}"

    def _range(self, sid):
        start = None
        for i, l in enumerate(self.lines):
            if re.match(rf"^##\s+{sid}\s*·", l):
                start = i
            elif start is not None and l.startswith("## "):
                return start, i
        return (start, len(self.lines)) if start is not None else (None, None)

    def statement(self, sid):
        a, _ = self._range(sid)
        if a is None:
            return None
        return re.sub(rf"^##\s+{sid}\s*·\s*", "", self.lines[a]).strip()

    def amend_statement(self, sid, new_statement):
        a, _ = self._range(sid)
        if a is None:
            raise KeyError(sid)
        self.lines[a] = f"## {sid} · {new_statement}"

    def set_status(self, sid, status):
        a, b = self._range(sid)
        if a is None:
            raise KeyError(sid)
        for i in range(a, b):
            if "status:" in self.lines[i]:
                self.lines[i] = re.sub(r"(status:\s*)\S+", rf"\g<1>{status}", self.lines[i], count=1)
                return
        raise ValueError(f"no status line in {sid}")

    def add_evidence(self, sid, d_for, d_against):
        a, b = self._range(sid)
        if a is None:
            raise KeyError(sid)
        for i in range(a, b):
            m = re.match(r"(\s*- evidence:\s*)(\d+)(\s*for\s*·\s*)(\d+)(\s*against)", self.lines[i])
            if m:
                self.lines[i] = (f"{m.group(1)}{int(m.group(2)) + d_for}{m.group(3)}"
                                 f"{int(m.group(4)) + d_against}{m.group(5)}")
                return
        raise ValueError(f"no evidence line in {sid}")

    def append_log(self, sid, date, text):
        a, b = self._range(sid)
        if a is None:
            raise KeyError(sid)
        marker = None
        for i in range(a, b):
            if re.match(r"\s*- (changelog|log):\s*$", self.lines[i]):
                marker = i
                break
        if marker is None:
            raise ValueError(f"no changelog/log marker in {sid}")
        end = marker
        for i in range(marker + 1, b):
            if re.match(r"\s{2,}- ", self.lines[i]):
                end = i
            else:
                break
        self.lines.insert(end + 1, f"  - {date}: {text}")

    def append_section(self, text):
        if self.lines and self.lines[-1].strip():
            self.lines.append("")
        self.lines.extend(text.rstrip().splitlines())

    def save(self):
        self.path.write_text("\n".join(self.lines).rstrip() + "\n", encoding="utf-8")


def new_principle_section(pid, statement, ptype, rigidity, scope, origin, date, log_note):
    return (f"## {pid} · {statement}\n"
            f"- type: {ptype} · rigidity: {rigidity} · scope: {scope}\n"
            f"- origin: {origin} · status: active\n"
            f"- evidence: 0 for · 0 against\n"
            f"- changelog:\n"
            f"  - {date}: {log_note}")


def new_hypothesis_section(hid, statement, prediction, falsifier, expiry, date):
    return (f"## {hid} · {statement}\n"
            f"- status: testing\n"
            f"- prediction: {prediction}\n"
            f"- falsifier: {falsifier}\n"
            f"- expiry: {expiry}\n"
            f"- evidence: 0 for · 0 against\n"
            f"- log:\n"
            f"  - {date}: Filed at reflection.")

"""Render a user-visible preview from an in-progress document draft."""


class DraftStreamRenderer:
    """Incrementally extract heading/body markdown from streamed Draft JSON."""

    def __init__(self, task, repository, clock):
        self.task = task
        self.repository = repository
        self.clock = clock
        self.reset()

    def reset(self):
        self.task.answer = None
        self._buf = ''
        self._pos = 0
        self._in_str = False
        self._escape = False
        self._unicode = None
        self._str_buf = []
        self._expect_key = True
        self._current_key = None
        self._active_field = None
        self._out = []
        self._last_saved = 0.0

    def _emit_char(self, ch: str):
        if self._active_field in ('heading', 'body'):
            self._out.append(ch)
        else:
            self._str_buf.append(ch)

    def feed(self, chunk: str):
        if not chunk:
            return
        self._buf += chunk
        while self._pos < len(self._buf):
            ch = self._buf[self._pos]
            self._pos += 1
            if self._in_str:
                if self._unicode is not None:
                    self._unicode += ch
                    if len(self._unicode) == 4:
                        try:
                            self._emit_char(chr(int(self._unicode, 16)))
                        except ValueError:
                            pass
                        self._unicode = None
                    continue
                if self._escape:
                    self._escape = False
                    if ch == 'n':
                        self._emit_char('\n')
                    elif ch == 't':
                        self._emit_char('\t')
                    elif ch == 'r':
                        continue
                    elif ch == 'u':
                        self._unicode = ''
                    else:
                        self._emit_char(ch)
                    continue
                if ch == '\\':
                    self._escape = True
                    continue
                if ch == '"':
                    self._in_str = False
                    if self._active_field == 'heading':
                        self._out.append('\n\n')
                        self._active_field = None
                    elif self._active_field == 'body':
                        self._out.append('\n\n')
                        self._active_field = None
                    elif self._expect_key:
                        self._current_key = ''.join(self._str_buf)
                    self._str_buf.clear()
                    continue
                self._emit_char(ch)
                continue
            if ch == '"':
                self._in_str = True
                self._str_buf.clear()
                if not self._expect_key and self._current_key in ('heading', 'body'):
                    self._active_field = self._current_key
                    if self._current_key == 'heading':
                        self._out.append('### ')
                continue
            if ch == ':':
                self._expect_key = False
            elif ch in (',', '{', '['):
                self._expect_key = (ch == '{') or (ch == ',')
                if ch == ',':
                    self._current_key = None
            elif ch in ('}', ']'):
                self._expect_key = False
                self._current_key = None
        rendered = ''.join(self._out).rstrip()
        if rendered and rendered != (self.task.answer or ''):
            self.task.answer = rendered
            now = self.clock()
            if now - self._last_saved >= 0.2:
                self._last_saved = now
                self.repository.save(self.task)

    def flush(self):
        rendered = ''.join(self._out).rstrip()
        if rendered:
            self.task.answer = rendered
            self.repository.save(self.task)

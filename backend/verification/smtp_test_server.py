"""Loopback-only SMTP fixture: retains messages in memory, never relays them."""
from socketserver import StreamRequestHandler, ThreadingTCPServer


class LocalSMTPHandler(StreamRequestHandler):
    def respond(self, value):
        self.wfile.write(value+b"\r\n")
        self.wfile.flush()

    def handle(self):
        self.respond(b"220 localhost test SMTP")
        while line := self.rfile.readline(65536):
            verb = line.split(b" ",1)[0].strip().upper()
            if verb in {b"EHLO",b"HELO",b"MAIL",b"RCPT",b"RSET"}:
                self.respond(b"250 OK")
            elif verb == b"DATA":
                self.respond(b"354 End with dot")
                data=[]
                while row := self.rfile.readline(65536):
                    if row == b".\r\n":
                        break
                    data.append(row[1:] if row.startswith(b"..") else row)
                self.server.received.append(b"".join(data))
                if self.server.mode == "drop":
                    return
                self.respond(b"550 rejected" if self.server.mode == "reject" else b"250 accepted")
            elif verb == b"QUIT":
                if self.server.mode != "quit_drop":
                    self.respond(b"221 closing")
                return
            else:
                self.respond(b"500 unsupported")


class LocalSMTPServer(ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

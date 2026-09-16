"""Email: .eml and .msg-adjacent formats, plus .mbox.

The stdlib `email` package is a genuinely good MIME parser, so the work here
is not parsing -- it is deciding what "the text of this email" means when a
message carries a plain-text part, an HTML part, and four attachments.

Policy: prefer text/plain; fall back to stripped HTML; list attachment names
rather than silently dropping them, because "was there a spreadsheet attached"
is usually the question being asked.
"""

import email
import email.policy

from .markup import strip_html


def _body_text(msg):
    """Best-effort body of a message, preferring text/plain over text/html."""
    plain, html = [], []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if part.get_filename():
            continue                      # attachment, handled separately
        ctype = part.get_content_type()
        try:
            payload = part.get_content()
        except (LookupError, UnicodeDecodeError):
            raw = part.get_payload(decode=True) or b""
            payload = raw.decode("utf-8", errors="replace")
        if ctype == "text/plain":
            plain.append(payload)
        elif ctype == "text/html":
            html.append(payload)
    if any(p.strip() for p in plain):
        return "\n".join(plain).strip()
    if html:
        return strip_html("\n".join(html)).strip()
    return ""


def _attachment_names(msg):
    names = []
    for part in msg.walk():
        name = part.get_filename()
        if name:
            names.append(name)
    return names


def _render(msg):
    head = []
    for field in ("From", "To", "Cc", "Date", "Subject"):
        value = msg.get(field)
        if value:
            head.append(f"{field}: {value}")
    body = _body_text(msg)
    attachments = _attachment_names(msg)
    out = "\n".join(head)
    if body:
        out += "\n\n" + body
    if attachments:
        out += "\n\nAttachments: " + ", ".join(attachments)
    return out


def extract_eml(fh):
    """Headers, body and attachment names of a single RFC 822 message."""
    data = fh.read() if hasattr(fh, "read") else fh
    msg = email.message_from_bytes(data, policy=email.policy.default)
    return _render(msg)


def extract_mbox(fh):
    """Every message in an mbox, separated by a marker line."""
    data = fh.read() if hasattr(fh, "read") else fh
    text = data.decode("utf-8", errors="replace")
    # mbox delimits on a line starting "From " at column 0.
    chunks, current = [], []
    for line in text.splitlines(keepends=True):
        if line.startswith("From ") and current:
            chunks.append("".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        chunks.append("".join(current))
    out = []
    for i, chunk in enumerate(chunks, 1):
        msg = email.message_from_string(chunk, policy=email.policy.default)
        out.append(f"--- message {i} ---")
        out.append(_render(msg))
    return "\n".join(out)

from django.core.mail.backends.base import BaseEmailBackend
from email.header import decode_header, make_header


class DecodedConsoleBackend(BaseEmailBackend):
    """A small email backend that prints decoded subject and text parts.

    This is useful during development on environments where the default
    `console.EmailBackend` prints raw MIME (with base64) which is hard to
    read for non-ASCII content.
    """

    def send_messages(self, email_messages):
        if not email_messages:
            return 0

        sent = 0
        for message in email_messages:
            try:
                msg = message.message()
            except Exception:
                # Fall back: message may already be a string-like
                print(str(message))
                sent += 1
                continue

            # Decode Subject
            raw_subj = msg.get('Subject', '')
            try:
                subj = str(make_header(decode_header(raw_subj)))
            except Exception:
                subj = raw_subj

            print(f"From: {msg.get('From')}")
            print(f"To: {msg.get('To')}")
            print(f"Subject: {subj}")

            # Print text/plain parts (decoded)
            if msg.is_multipart():
                for part in msg.walk():
                    ctype = part.get_content_type()
                    if ctype == 'text/plain':
                        payload = part.get_payload(decode=True)
                        if payload is None:
                            print(part.get_payload())
                            continue
                        charset = part.get_content_charset() or 'utf-8'
                        try:
                            text = payload.decode(charset, errors='replace')
                        except Exception:
                            text = payload.decode('utf-8', errors='replace')
                        print(text)
            else:
                payload = msg.get_payload(decode=True)
                if payload is None:
                    print(msg.get_payload())
                else:
                    charset = msg.get_content_charset() or 'utf-8'
                    try:
                        text = payload.decode(charset, errors='replace')
                    except Exception:
                        text = payload.decode('utf-8', errors='replace')
                    print(text)

            print('-' * 78)
            sent += 1

        return sent

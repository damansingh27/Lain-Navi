"""
Local Microsoft Outlook integration via COM (`win32com.client`).

Uses the default MAPI inbox, sorted by ReceivedTime. Functions return plain strings
suitable for LLM or TTS. `read_email_by_id` / `get_latest_email` return full body text
(up to an internal cap) for voice readout; list helpers use short previews only.
"""

import win32com.client

# Last message EntryID touched by read/get — used for `read_last_email`.
_last_entry_id = None


def _normalize_body(text):
    """Whitespace-normalize for display/TTS (single spaces, no raw newlines)."""
    return " ".join((text or "").replace("\r", " ").replace("\n", " ").split())


def _compact_text(text, max_len=200):
    """One-line preview for inbox lists (not used for full `read_email_by_id`)."""
    clean = _normalize_body(text)
    return clean[:max_len]


def _get_outlook_namespace():
    """Return the Outlook MAPI namespace, or (None, error_string) on failure."""
    try:
        app = win32com.client.Dispatch("Outlook.Application")
        return app.GetNamespace("MAPI")
    except Exception as e:
        return None, f"Could not connect to Outlook desktop app: {e}"


def _inbox_items():
    """Default inbox `Items` collection sorted newest-first, or (None, error)."""
    namespace = _get_outlook_namespace()
    if isinstance(namespace, tuple):
        return None, namespace[1]
    inbox = namespace.GetDefaultFolder(6)  # Inbox
    items = inbox.Items
    items.Sort("[ReceivedTime]", True)
    return items, None


def get_unread_emails(count=5):
    """Returns unread emails from local Outlook inbox."""
    items, err = _inbox_items()
    if err:
        return err
    unread = []
    for msg in items:
        if len(unread) >= int(count):
            break
        try:
            if msg.UnRead:
                sender = msg.SenderName or "Unknown sender"
                subject = msg.Subject or "(no subject)"
                received = msg.ReceivedTime.strftime("%A %I:%M %p")
                preview = _compact_text(msg.Body)
                unread.append(f"{sender} | {received} | {subject} | unread | {preview}")
        except Exception:
            continue
    if not unread:
        return "No unread emails."
    return "\n".join(unread)


def get_recent_emails(count=5):
    """Returns recent inbox emails regardless of read status."""
    global _last_entry_id
    items, err = _inbox_items()
    if err:
        return err
    output = []
    for msg in items:
        if len(output) >= int(count):
            break
        try:
            sender = msg.SenderName or "Unknown sender"
            subject = msg.Subject or "(no subject)"
            received = msg.ReceivedTime.strftime("%A %I:%M %p")
            unread = "unread" if msg.UnRead else "read"
            preview = _compact_text(msg.Body)
            output.append(f"{sender} | {received} | {subject} | {unread} | {preview}")
            if not _last_entry_id:
                _last_entry_id = msg.EntryID
        except Exception:
            continue
    if not output:
        return "No emails found."
    return "\n".join(output)


def get_latest_email():
    """Returns full content for latest inbox email."""
    items, err = _inbox_items()
    if err:
        return err
    for msg in items:
        try:
            return read_email_by_id(msg.EntryID, max_body_chars=None)
        except Exception:
            continue
    if not items:
        return "No emails found."
    return "No emails found."


def read_email(subject_query):
    """Reads most recent email whose subject contains subject_query."""
    items, err = _inbox_items()
    if err:
        return err
    normalized = (subject_query or "").strip().lower()
    for msg in items:
        try:
            if normalized in (msg.Subject or "").lower():
                return read_email_by_id(msg.EntryID, max_body_chars=None)
        except Exception:
            continue
    return f"No email found matching: {subject_query}"


# Max body length when max_body_chars=None (guards against huge HTML→text dumps).
_FULL_BODY_CAP = 200_000


def read_email_by_id(message_id, max_body_chars=None):
    """
    Reads a specific email by Outlook EntryID.
    max_body_chars: None = full body up to _FULL_BODY_CAP; int = preview limit (list views).
    """
    global _last_entry_id
    namespace = _get_outlook_namespace()
    if isinstance(namespace, tuple):
        return namespace[1]
    if not message_id:
        return "Message id is required."
    try:
        msg = namespace.GetItemFromID(message_id)
    except Exception as e:
        return f"Could not open message id {message_id}: {e}"
    _last_entry_id = msg.EntryID
    sender = msg.SenderName or "Unknown sender"
    subject = msg.Subject or "(no subject)"
    try:
        received = msg.ReceivedTime.strftime("%A %I:%M %p")
    except Exception:
        received = "Unknown time"
    clean = _normalize_body(msg.Body or "")
    cap = _FULL_BODY_CAP if max_body_chars is None else int(max_body_chars)
    body_text = clean[:cap]
    if len(clean) > cap:
        body_text += "\n\n[Body truncated for length.]"
    return f"From: {sender}\nDate: {received}\nSubject: {subject}\n\n{body_text}"


def read_last_email():
    """Re-reads the last email accessed in this session."""
    if not _last_entry_id:
        return "No email has been accessed yet this session."
    return read_email_by_id(_last_entry_id, max_body_chars=None)


def send_email(to_address, subject, body):
    """Sends an email through local Outlook desktop app."""
    try:
        app = win32com.client.Dispatch("Outlook.Application")
        mail = app.CreateItem(0)
        mail.To = to_address
        mail.Subject = subject
        mail.Body = body
        mail.Send()
        return f"Email sent to {to_address}."
    except Exception as e:
        return f"Failed to send email: {e}"


def create_draft(to_address, subject, body):
    """Creates a draft in local Outlook."""
    try:
        app = win32com.client.Dispatch("Outlook.Application")
        mail = app.CreateItem(0)
        mail.To = to_address
        mail.Subject = subject
        mail.Body = body
        mail.Save()
        return f"Draft created. id={mail.EntryID}"
    except Exception as e:
        return f"Failed to create draft: {e}"


def flag_email(message_id, flag_status="flagged"):
    """Flags an Outlook message by EntryID."""
    namespace = _get_outlook_namespace()
    if isinstance(namespace, tuple):
        return namespace[1]
    mapping = {"notFlagged": 0, "complete": 1, "flagged": 2}
    if flag_status not in mapping:
        return "Invalid flag status. Use flagged, complete, or notFlagged."
    try:
        msg = namespace.GetItemFromID(message_id)
        msg.FlagStatus = mapping[flag_status]
        msg.Save()
        return f"Message flagged: {flag_status}."
    except Exception as e:
        return f"Failed to flag message: {e}"


def move_email(message_id, destination_folder="Archive"):
    """Moves an Outlook message by EntryID to a target folder in mailbox root."""
    namespace = _get_outlook_namespace()
    if isinstance(namespace, tuple):
        return namespace[1]
    try:
        msg = namespace.GetItemFromID(message_id)
        inbox = namespace.GetDefaultFolder(6)
        target_folder = None
        for folder in inbox.Parent.Folders:
            if (folder.Name or "").strip().lower() == destination_folder.strip().lower():
                target_folder = folder
                break
        if target_folder is None:
            return f"Folder not found: {destination_folder}"
        msg.Move(target_folder)
        return f"Message moved to {target_folder.Name}."
    except Exception as e:
        return f"Failed to move message: {e}"


def get_emails_from_sender(sender_name, count=3):
    """
    Compatibility helper retained for existing tool prompts.
    Uses recent-message scan and sender-name filtering.
    """
    items, err = _inbox_items()
    if err:
        return err
    filtered = []
    needle = (sender_name or "").lower()
    for msg in items:
        try:
            sender = msg.SenderName or ""
            if needle in sender.lower():
                subject = msg.Subject or "(no subject)"
                received = msg.ReceivedTime.strftime("%A %I:%M %p")
                unread = "unread" if msg.UnRead else "read"
                preview = _compact_text(msg.Body)
                filtered.append(f"{sender} | {received} | {subject} | {unread} | {preview}")
                if len(filtered) >= int(count):
                    break
        except Exception:
            continue
    if not filtered:
        return f"No emails found from {sender_name}."
    return "\n".join(filtered)


def mark_all_read():
    """
    Compatibility helper retained for existing tool prompts.
    Marks top unread inbox messages as read in batches.
    """
    items, err = _inbox_items()
    if err:
        return err
    updated = 0
    for msg in items:
        try:
            if msg.UnRead:
                msg.UnRead = False
                msg.Save()
                updated += 1
        except Exception:
            continue
    if not updated:
        return "No unread emails to mark."
    return f"Marked {updated} emails as read."
"""Bounded multipart parsing shared by AGCore upload endpoints."""

from typing import Dict, Optional

from fastapi import HTTPException


def parse_multipart_upload(
    content_type: str, body: bytes, *, single_file: bool = False,
) -> tuple[str, Optional[str], bytes, Dict[str, str]]:
    """Stdlib multipart/form-data parser avoiding external python-multipart dependency."""
    from email.parser import BytesParser
    from email.policy import default as email_default

    raw_msg = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8") + body
    msg = BytesParser(policy=email_default).parsebytes(raw_msg)
    if not msg.is_multipart():
        raise HTTPException(status_code=400, detail={"code": "invalid_request", "message": "multipart/form-data required"})

    filename = "upload.bin"
    file_mime: Optional[str] = None
    file_bytes: Optional[bytes] = None
    fields: Dict[str, str] = {}
    for part in msg.iter_parts():
        field_name = part.get_param("name", header="content-disposition")
        part_filename = part.get_filename()
        if single_file and (field_name != "file" or not part_filename or file_bytes is not None):
            raise HTTPException(status_code=422, detail="attachment_requires_one_file")
        payload = part.get_payload(decode=True) or b""
        if field_name == "file" or part_filename is not None:
            filename = part_filename or filename
            file_mime = part.get_content_type()
            file_bytes = payload
        elif field_name:
            fields[str(field_name)] = payload.decode("utf-8", errors="replace")
    if file_bytes is None:
        raise HTTPException(status_code=422, detail={"code": "invalid_request", "message": "missing multipart file field"})
    return filename, file_mime, file_bytes, fields


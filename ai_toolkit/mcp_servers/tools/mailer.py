import os
import base64

import markdown
import msal
import mimetypes
import requests

from common_utils_repository.config.constant import (
    USERIDJSON,
    TENANTID,
    CLIENTID,
    CLIENTSECRET,
)

# ------------------------------------------------------------------------------------
# Convert markdown file to html 
# ------------------------------------------------------------------------------------

def markdown_report_to_html(markdown_text: str) -> str:
    """
    Convert a markdown LLM report (heading, tables, code fences, bold
    lables, lists) into an html fragment suitable for an html body
    """
    return markdown.markdown(markdown_text, 
                             extensions=["tables", "fenced_code", "nlbr", "sana_lists"])


# CSS for the elements markdown.markdown() emits (plain tags, no classes).
# Kept as a <style> block since Outlook/Graph renders it via the Word
# engine; if you later need to support other webmail clients, run this
# through a CSS-inliner (e.g. premailer) before sending.
REPORT_STYLE = """
    table { border-collapse: collapse; width: 100%; margin: 10px 0; font-family: Arial, sans-serif; font-size: 13px; }
    th, td { border: 1px solid #cccccc; padding: 6px 8px; text-align: left; vertical-align: top; }
    th { background-color: #1F4E79; color: #ffffff; }
    h1, h2, h3 { color: #1F4E79; font-family: Arial, sans-serif; margin-top: 20px; margin-bottom: 6px; }
    hr { border: none; border-top: 1px solid #cccccc; margin: 16px 0; }
    pre { background-color: #f2f2f2; padding: 10px; overflow-x: auto; font-family: Consolas, monospace; font-size: 12.5px; border-radius: 4px; }
    code { font-family: Consolas, monospace; font-size: 12.5px; }
    ul, ol { margin: 4px 0 10px 20px; }
"""


# -------------------------------------------------------------------------
# Attachment handling
# --------------------------------------------------------------------------
def create_attachment(attachment_path):
    attachment_name = attachment_path.split('/')[-1]
    content_type, _ = mimetypes.guess_type(attachment_path)
    if not content_type:
        content_type = "application/octet-stream"
    
    with open(attachment_path, 'rb') as attachment_file:
        attachment_content = base64.b64encode(attachment_file.read()).decode(encoding='utf-8')
    
    return {
        '@odata.type': "#microsoft.graph.fileAttachment",
        "name": attachment_name,
        "contentBytes": attachment_content,
        "contentTypes": content_type
    }

# -------------------------------------------------------------------------
# Convert attachment paths in list
# --------------------------------------------------------------------------

def _normalize_attachment_paths(attachment_paths):
    """Accept None, a single path string, or a list of path strings."""
    if not attachment_paths:
        return []
    if isinstance(attachment_paths, str):
        return [attachment_paths]
    return list(attachment_paths)

# -------------------------------------------------------------------------
# Mailer
# --------------------------------------------------------------------------

def send_mailer(
        sender="partner",
        recipient= "",
        recipient_cc = "",
        recipient_bcc ="",
        subject="",
        body="",
        markdown_body=None,
        attachement_paths = [],
):
    """
    Send an email """

    user = next(
        (value for value in USERIDJSON if value['sender'] == sender), 
        None,
    )

    if user is None:
        raise ValueError(
            f"Unknown sender '{sender}'. Avaliable senders: "
            f"{[value['sender'] for value in USERIDJSON]}"
        )
    
    USERID = user["id"]

    def get_access_token():
        authority = f"https://login.microsoftonline.com/{TENANTID}"
        scopes = ["https://graph.microsoft.com/.default"]
        app = msal.ConfidentialClientApplication(
            CLIENTID, client_credential=CLIENTSECRET, authority= authority
        )
        result = app.acquire_token_silent(scopes, account=None)
        if not result:
            app.acquire_token_for_client(scopes=scopes)
        return result.get("access_token")
    
    def constract_email():
        if markdown_body is not None:
            content_html = markdown_report_to_html(markdown_text=markdown_body)
        else:
            content_html = f'<p class="normal small">{body}</p>'

        html_content = f"""
            <!DOCTYPE html>
            <html>
            <head>
            <style>
                .normal {{font-family: Arial, sans-serif; font-size: 14px;}}
                .bold {{font-weight: bold;}}
                .italic {{font-style: italic;}}
                .large {{font-size: 18px;}}
                .small {{font-size: 12px;}}
                .table-style {{border-collapse: collapse; border: 1px solid black; padding: 8px;}}
                .signature {{margin-top: 20px;}}
                {REPORT_STYLE}
            </style>
            </head>
            <body>
                {content_html}

                <br>
                <div class="signature">
                    <p class="bold">Thanks and Regards,<br>Infytrix Ecom Private Limited</p>
                    <img src="cid:infytrix-logo" height="108" width="205">
                </div>
            </body>
            </html>
        """

        # with open(str(CURRENT_DIR + "/resource/" + "infytrix-logo.png"), "rb") as image_file:
        #     inline_image_content = base64.b64encode(image_file.read()).decode("utf-8")

        message_data = {
            "message": {
                "subject": subject,
                "body": {"contentType": "html", "content": html_content},
                "attachments": [
                    {
                        "@odata.type": "#microsoft.graph.fileAttachment",
                        "name": "infytrix-logo.png" or None,
                        # "contentBytes": inline_image_content,
                        "contentType": "image/png",
                        "isInline": True,
                        "contentId": "infytrix-logo",
                    }
                ],
            },
            "saveToSentItems": "true",
        }

        if recipient:
            message_data["message"]["toRecipients"] = [
                {"emailAddress": {"address": addr}} for addr in recipient.split(',')]
        if recipient_cc:
            message_data["message"]["ccRecipients"] = [
                {"emailAddress": {"address": addr} for addr in recipient_cc.split(',')}
            ]
        if recipient_bcc:
            message_data["message"]["bccRecipients"] = [
                {"emailAddress": {"address": addr}} for addr in recipient_bcc.split(',')
            ]
        for path in _normalize_attachment_paths(attachment_paths=attachement_paths):
            message_data['message']['attachments'].append(create_attachment(path))
        
        return message_data
        
    token = get_access_token()
    if not token:
        return {"sucess":False, "error": "Failed to acquire acess token."}
    
    message_data = constract_email()

    response = requests.post(
        f"https://graph.microsoft.com/v1.0/users/{USERID}/sendMail",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=message_data,
    )

    if response.status_code == 202:
        return {"success": True}
    return {"success": False, "error": f"{response.status_code}: {response.text}"}


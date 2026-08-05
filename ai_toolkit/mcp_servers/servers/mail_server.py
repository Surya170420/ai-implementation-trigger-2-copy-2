"""
mail_server.py

Expose the infytrix mailer function as MCP tool so any MCP-compatible client 
(claude desktop, claude code or our own local model and any other mcp supporting model) send there
 report via email just via add mail_server MCP config
 
Install once:
    pip install mcp markdown msal requests

Run:
    python mail_server.py
    

Then point an MCP client at this script over stdio. For Claude
Desktop / Claude Code, add to your MCP config:

{
    "mcpservers":{
        "send_mail":{
        "command": "python",
        "args": ["absolute/path/to/mail_server.py"]
        }
    }
}
"""
from typing import Optional
from mcp.server.fastmcp import FastMCP, List

from ai_toolkit.mcp_servers.tools.mailer import send_mailer

# Initialize the mcp server
mcp = FastMCP("send_mail")

@mcp.tool()
def send_report_email(
    subject: str,
    markdown_body: str,
    recipient: str,
    recipient_cc: str = "", 
    recipient_bcc: str = "",
    sender: str = "partner",
    attachment_paths: Optional[list[str]] = None,

)-> str:
    """
    Send an LLM-generated markdown report as a formatted HTML email.

    Use this for reports that contain headings, tables, code blocks, or
    bold labels (e.g. the scraper validation issues report) — the
    markdown is rendered into proper HTML so it displays correctly in
    the recipient's inbox instead of showing raw '**'/'###' symbols.

    Args:
        subject: Email subject line.
        markdown_body: The full markdown report text (e.g. the LLM's
            summary_prompt output).
        recipient: Comma-separated "To" addresses.
        recipient_cc: Comma-separated "Cc" addresses.
        recipient_bcc: Comma-separated "Bcc" addresses.
        sender: Which configured mailbox to send from (must match a
            "sender" entry in USERIDJSON). Defaults to "partner".
        attachment_paths: Absolute file paths to attach — e.g. a
            generated .docx report plus any other supporting files.
    """
    result =  send_mailer(
        sender=sender,
        subject=subject,
        recipient=recipient,
        recipient_cc=recipient_cc,
        recipient_bcc=recipient_bcc,
        markdown_body=markdown_body,
        attachement_paths=attachment_paths)

    if result.get("success"):
        return f"Result sent to {recipient} with subject '{subject}'"
    return f"Failed to sent: {result.get("error")}"

@mcp.tool()
def send_plain_email(
    subject: str,
    body: str,
    recipient: str,
    recipient_cc: str = "",
    recipient_bcc: str = "",
    sender: str = "partner",
    attachment_paths: Optional[List[str]] = None,
) -> str:
    """
    Send a plain-text email (no markdown rendering) with optional attachments.
    Use this for simple messages that aren't structured LLM reports.
    """
    result = send_mailer(
        sender=sender,
        recipient=recipient,
        recipient_cc=recipient_cc,
        recipient_bcc=recipient_bcc,
        subject=subject,
        body=body,
        attachment_paths=attachment_paths,
    )
    if result.get("success"):
        return f"Email sent to {recipient} with subject '{subject}'."
    return f"Failed to send email: {result.get('error')}"

if __name__ == "__main__":
    mcp.run()
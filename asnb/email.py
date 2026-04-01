import smtplib
import ssl
import os
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from typing import Optional, Dict, Any, List

class EmailNotificationError(Exception):
    """Custom exception for email notification errors."""
    pass

def send_purchase_notification(
    email_config: Dict[str, Any],
    success: bool,
    fund_name: Optional[str] = None,
    amount: Optional[str] = None,
    error_message: Optional[str] = None,
    screenshot_paths: Optional[List[str]] = None
) -> bool:
    """
    Sends an email notification about ASNB purchase attempt.
    
    Args:
        email_config: Dictionary containing email configuration
        success: Whether the purchase was successful
        fund_name: Name of the fund attempted
        amount: Amount attempted to purchase
        error_message: Error message if purchase failed
        screenshot_paths: Optional list of screenshot file paths to attach
    
    Returns:
        True if email sent successfully, False otherwise
    """
    if not email_config:
        print("Email notifications disabled - no email configuration found.")
        return False
    
    # Check if we should send email based on success/failure settings
    if success and not email_config.get('send_on_success', True):
        print("Success email notifications disabled.")
        return False
    
    if not success and not email_config.get('send_on_failure', False):
        print("Failure email notifications disabled.")
        return False
    
    try:
        # Handle multiple recipients
        recipient_emails = email_config.get('recipient_emails', [])
        if not recipient_emails:
            # Fallback for old config format
            single_recipient = email_config.get('recipient_email', '')
            if single_recipient:
                recipient_emails = [single_recipient]
            else:
                print("No recipient emails configured.")
                return False
        
        # Defensive programming: Ensure recipient_emails is always a proper list
        if isinstance(recipient_emails, str):
            # Handle case where it might be a comma-separated string
            recipient_emails = [email.strip() for email in recipient_emails.split(',') if email.strip()]
            print(f"DEBUG: Converted string to list: {recipient_emails}")
        elif not isinstance(recipient_emails, list):
            recipient_emails = [str(recipient_emails)]
            print(f"DEBUG: Converted to list: {recipient_emails}")
        
        # Ensure all items in the list are strings (not bytes or other types)
        recipient_emails = [str(email).strip() for email in recipient_emails if str(email).strip()]
        
        # Validate email addresses contain @ symbol (basic validation)
        valid_emails = []
        for email in recipient_emails:
            if '@' in email and '.' in email:
                valid_emails.append(email)
            else:
                print(f"WARNING: Invalid email format skipped: {email}")
        
        recipient_emails = valid_emails
        if not recipient_emails:
            print("ERROR: No valid email addresses found after validation.")
            return False
            
        print(f"DEBUG: Final recipient_emails after cleanup and validation: {recipient_emails}")
        print(f"DEBUG: Number of recipients: {len(recipient_emails)}")
        
        # Create message
        message = MIMEMultipart("alternative")
        message["Subject"] = email_config.get('email_subject', 'ASNB Purchase Notification')
        message["From"] = email_config['sender_email']
        message["To"] = ", ".join(recipient_emails)
        
        # Create email content
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        if success:
            subject_status = "✅ SUCCESS"
            html_color = "#28a745"  # Green
            text_content = f"""
ASNB Purchase Successful!

Fund: {fund_name or 'Unknown'}
Amount: RM {amount or 'Unknown'}
Time: {timestamp}

The purchase has reached the final payment confirmation page.
Please complete the payment manually in your browser.

This is an automated notification from your ASNB Buyer script.
"""
            html_content = f"""
<html>
  <body>
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
      <h2 style="color: {html_color};">🎉 ASNB Purchase Successful!</h2>
      
      <div style="background-color: #f8f9fa; padding: 20px; border-radius: 5px; margin: 20px 0;">
        <h3>Purchase Details:</h3>
        <p><strong>Fund:</strong> {fund_name or 'Unknown'}</p>
        <p><strong>Amount:</strong> RM {amount or 'Unknown'}</p>
        <p><strong>Time:</strong> {timestamp}</p>
      </div>
      
      <div style="background-color: #fff3cd; padding: 15px; border-radius: 5px; border-left: 4px solid #ffc107;">
        <p><strong>⚠️ Action Required:</strong><br>
        The purchase has reached the final payment confirmation page.<br>
        Please complete the payment manually in your browser.</p>
      </div>
      
      <hr style="margin: 30px 0;">
      <p style="color: #6c757d; font-size: 12px;">
        This is an automated notification from your ASNB Buyer script.
      </p>
    </div>
  </body>
</html>
"""
        else:
            subject_status = "❌ FAILED"
            html_color = "#dc3545"  # Red
            text_content = f"""
ASNB Purchase Failed

Fund: {fund_name or 'Unknown'}
Amount: RM {amount or 'Unknown'}
Time: {timestamp}

Error: {error_message or 'Unknown error occurred'}

The script will continue trying other funds if configured.

This is an automated notification from your ASNB Buyer script.
"""
            html_content = f"""
<html>
  <body>
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
      <h2 style="color: {html_color};">❌ ASNB Purchase Failed</h2>
      
      <div style="background-color: #f8f9fa; padding: 20px; border-radius: 5px; margin: 20px 0;">
        <h3>Attempted Purchase:</h3>
        <p><strong>Fund:</strong> {fund_name or 'Unknown'}</p>
        <p><strong>Amount:</strong> RM {amount or 'Unknown'}</p>
        <p><strong>Time:</strong> {timestamp}</p>
      </div>
      
      <div style="background-color: #f8d7da; padding: 15px; border-radius: 5px; border-left: 4px solid #dc3545;">
        <p><strong>Error:</strong><br>
        {error_message or 'Unknown error occurred'}</p>
      </div>
      
      <p>The script will continue trying other funds if configured.</p>
      
      <hr style="margin: 30px 0;">
      <p style="color: #6c757d; font-size: 12px;">
        This is an automated notification from your ASNB Buyer script.
      </p>
    </div>
  </body>
</html>
"""
        
        # Update subject with status (must delete first to avoid duplicate headers)
        base_subject = message["Subject"]
        del message["Subject"]
        message["Subject"] = f"{subject_status} - {base_subject}"
        
        # Create the plain-text and HTML versions of the message
        text_part = MIMEText(text_content, "plain")
        html_part = MIMEText(html_content, "html")
        
        # Add parts to message
        message.attach(text_part)
        message.attach(html_part)
        
        # Attach screenshots if provided
        if screenshot_paths:
            for i, screenshot_path in enumerate(screenshot_paths):
                if os.path.exists(screenshot_path):
                    try:
                        with open(screenshot_path, 'rb') as f:
                            img_data = f.read()
                        image = MIMEImage(img_data)
                        # Use filename from path or generate one
                        filename = os.path.basename(screenshot_path)
                        if not filename:
                            filename = f"screenshot_{i+1}.png"
                        image.add_header('Content-Disposition', f'attachment; filename="{filename}"')
                        image.add_header('Content-ID', f'<screenshot{i+1}>')
                        message.attach(image)
                        print(f"📎 Attached screenshot: {filename}")
                    except Exception as img_err:
                        print(f"Warning: Could not attach screenshot {screenshot_path}: {img_err}")
                else:
                    print(f"Warning: Screenshot file not found: {screenshot_path}")
        
        # Send email
        recipients_list = ", ".join(recipient_emails)
        print(f"Sending email notification to: {recipients_list}")
        
        # Debug: Check recipient format
        print(f"DEBUG: recipient_emails type: {type(recipient_emails)}")
        print(f"DEBUG: recipient_emails value: {recipient_emails}")
        print(f"DEBUG: recipient_emails length: {len(recipient_emails)}")
        for i, email in enumerate(recipient_emails):
            print(f"DEBUG: recipient[{i}]: '{email}' (type: {type(email)})")
        
        # Create secure SSL context
        context = ssl.create_default_context()
        
        # Connect to server and send email
        with smtplib.SMTP(email_config['smtp_server'], email_config['smtp_port']) as server:
            # Enable debug output from SMTP
            server.set_debuglevel(1)
            server.starttls(context=context)
            server.login(email_config['sender_email'], email_config['sender_password'])
            
            # Debug: Verify what we're passing to sendmail
            print(f"DEBUG: sendmail from_addr: '{email_config['sender_email']}'")
            print(f"DEBUG: sendmail to_addrs: {recipient_emails} (type: {type(recipient_emails)})")
            print(f"DEBUG: Message 'To' header: '{message['To']}'")
            
            # Send email and capture any rejection info
            try:
                refused = server.sendmail(
                    email_config['sender_email'],
                    recipient_emails,  # Pass list of recipients
                    message.as_string()
                )
                if refused:
                    print(f"WARNING: Some recipients were refused by server: {refused}")
                else:
                    print(f"DEBUG: All {len(recipient_emails)} recipients accepted by SMTP server")
                    
            except smtplib.SMTPRecipientsRefused as e:
                print(f"ERROR: All recipients refused: {e}")
                raise
            except smtplib.SMTPDataError as e:
                print(f"ERROR: SMTP data error: {e}")
                raise
        
        print(f"✅ Email notification sent successfully!")
        return True
        
    except Exception as e:
        print(f"❌ Failed to send email notification: {e}")
        return False

def test_email_config(email_config: Dict[str, Any]) -> bool:
    """
    Tests the email configuration by sending a test email.
    
    Args:
        email_config: Dictionary containing email configuration
    
    Returns:
        True if test email sent successfully, False otherwise
    """
    if not email_config:
        print("No email configuration to test.")
        return False
    
    print("Testing email configuration...")
    return send_purchase_notification(
        email_config=email_config,
        success=True,
        fund_name="Test Fund",
        amount="100",
        error_message=None
    )

# Example usage (for testing)
if __name__ == "__main__":
    # Example email config for testing
    test_config = {
        'smtp_server': 'smtp.gmail.com',
        'smtp_port': 587,
        'sender_email': 'test@example.com',
        'sender_password': 'password',
        'recipient_email': 'test@example.com',
        'send_on_success': True,
        'send_on_failure': True,
        'email_subject': 'ASNB Purchase Test'
    }
    
    print("This is a test module. Configure your email settings in config.ini to use.")
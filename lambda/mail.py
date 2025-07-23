
"""
Email functionality for Altair Alexa Skill
Provides SMTP email sending capabilities with proper error handling
"""

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dotenv import load_dotenv
import smtplib
import os
import logging

# Load environment variables
load_dotenv()

logger = logging.getLogger(__name__)


def server_smtp() -> smtplib.SMTP:
    """Create SMTP server connection"""
    smtp_server = os.getenv('EMAIL_HOST', 'smtp.gmail.com')
    smtp_port = int(os.getenv('EMAIL_PORT', '587'))

    try:
        # Connect to the SMTP server
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        return server
    except Exception as e:
        logger.error(f"Error connecting to SMTP server: {e}")
        raise


def connect_smtp() -> smtplib.SMTP:
    """Authenticate SMTP connection"""
    login = os.getenv('EMAIL_SENDER')
    password = os.getenv('EMAIL_PASSWORD')
    
    if not login or not password:
        raise ValueError("Email credentials not found in environment variables")
    
    try:
        server = server_smtp()
        server.login(login, password)
        return server
    except Exception as e:
        logger.error(f"Error authenticating SMTP connection: {e}")
        raise


def send_email(from_email, to_email, subject, body):
    """
    Send email via SMTP with validation and error handling
    
    Args:
        from_email (str): Sender email address
        to_email (str): Recipient email address  
        subject (str): Email subject
        body (str): Email body content
        
    Returns:
        str: Success or error message
    """
    # Basic email validation
    if not all([from_email, to_email, subject, body]):
        return "Erro: Todos os campos (remetente, destinatário, assunto, corpo) são obrigatórios"
    
    if '@' not in from_email or '@' not in to_email:
        return "Erro: Endereços de email inválidos"

    # Create the email message
    msg = MIMEMultipart()
    msg['From'] = from_email
    msg['To'] = to_email
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'plain', 'utf-8'))

    try:
        # Send the email
        server = connect_smtp()
        server.sendmail(from_email, to_email, msg.as_string())
        server.quit()
        logger.info(f"Email sent successfully from {from_email} to {to_email}")
        return f"Email enviado com sucesso para {to_email}"
    except ValueError as ve:
        logger.error(f"Configuration error: {ve}")
        return "Erro de configuração do email"
    except Exception as e:
        logger.error(f"Failed to send email: {e}")
        return f"Falha ao enviar email: {str(e)}"

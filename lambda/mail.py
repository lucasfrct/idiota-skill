
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dotenv import load_dotenv
import smtplib
import os


load_dotenv()


def server_smtp() -> smtplib.SMTP:

    smtp_server = os.getenv('EMAIL_HOST')
    smtp_port = os.getenv('EMAIL_PORT')

    # Connect to the SMTP server
    server = smtplib.SMTP(smtp_server, smtp_port)
    server.starttls()
    return server


def connect_smtp()-> smtplib.SMTP:

    login = os.getenv('EMAIL_SENDER')
    password = os.getenv('EMAIL_PASSWORD')
    
    server = server_smtp()
    server.login(login, password)
    
    return server


def send_email(from_email, to_email, subject, body):

    # Create the email message
    msg = MIMEMultipart()
    msg['From'] = from_email
    msg['To'] = to_email
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'plain'))

    try:
        # Send the email
        server = connect_smtp()
        server.sendmail(from_email, to_email, msg.as_string())
        server.quit()
        return f"Email enviado com sucesso para {to_email}"
    except Exception as e:
        print(f"Failed to send email: {e}")


# send_email("lucasfrct@gmail.com", "lucasfrct@outlook.com", "teste de título", "Teste de corpo de email")

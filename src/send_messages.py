import os
import requests
from dotenv import load_dotenv

load_dotenv()
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")

def send_message(to: str, message: str, phone_number_id: str):

    print("Sending message to", to)

    url = f"https://graph.facebook.com/v24.0/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "text": {"body": message}
    }

    print("Payload:", payload)

    response = requests.post(url, json=payload, headers=headers)
    print("Response post:", response.text)

    if response.status_code != 200:
        print(f"Failed to send message to {to}. Reponse: {response.status_code}")
    else:
        print(f"Message sent to {to}")
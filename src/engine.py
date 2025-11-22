from dotenv import load_dotenv

from src.send_messages import send_message
from src.sales_agent import agent

load_dotenv()

async def handle_message(message, phone_number_id):
    sender_id = message["from"]

    print("Body of the message:", message["text"]["body"])

    response = agent.invoke(
        {"messages": [{"role": "user", "content":  message["text"]["body"]}]},      
        {"configurable": { "thread_id": sender_id }}
    )

    print(response["messages"][-1].content)

    # Send a simple automated reply
    send_message(sender_id, response["messages"][-1].content, phone_number_id)
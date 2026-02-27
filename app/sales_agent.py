from dotenv import load_dotenv
from langchain.agents import create_agent
# from langgraph.checkpoint.postgres import PostgresSaver
from langchain.tools import tool
import os

load_dotenv()

products_stock = [
    {"product_id": "gas_001", "name": "Gas Propano", "price": 110, "stock": 3},
    {"product_id": "gas_002", "name": "Carton de huevos", "price": 19.99, "stock": 3},
]

# DB_URI = os.getenv("DATABASE_URL")

@tool
def get_product_stock():
    """Check the current stock of products."""
    available_products = [item for item in products_stock if item["stock"] > 0]
    if not available_products:
        return "All products are currently out of stock."
    response = "Available Products:\n"
    for product in available_products:
        response += f"- {product['name']} (ID: {product['product_id']}): ${product['price']} (Stock: {product['stock']})\n"
    return response

@tool
def sale_product(product_id: str, quantity: int):
    """Process the sale of a product by reducing its stock."""
    for product in products_stock:
        if product["product_id"] == product_id:
            if product["stock"] >= quantity:
                product["stock"] -= quantity
                return f"Sale successful: {quantity} units of {product['name']} sold. Remaining stock: {product['stock']}."
            else:
                return f"Insufficient stock for {product['name']}. Available stock: {product['stock']}."
    return "Product not found."



# with PostgresSaver.from_conn_string(DB_URI) as checkpointer:
#     checkpointer.setup()

agent = create_agent(
    model="gpt-4o-mini",
    system_prompt=(
        "Eres un asistente de ventas especializado en la venta de gas propano. "
        "Tu objetivo principal es ayudar a los clientes a realizar su compra de manera rápida y eficiente. "
        "Siempre debes corroborar que hay existencia de gas antes de proceder con la venta. "
        "Si un cliente pregunta por la existencia de gas, asume que está listo para comprar y guía la conversación hacia la venta directa. "
        "Sé claro, directo y profesional en tus respuestas. Si no hay suficiente stock, informa al cliente de manera cortés y ofrece alternativas si es posible."
        "Debes limitarte a dar información sobre el stock y no proporcionar cualquier otra información que no sea relevante para la venta."
    ),
    tools=[get_product_stock, sale_product],
    
)
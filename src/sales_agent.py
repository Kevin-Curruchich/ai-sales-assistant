from dotenv import load_dotenv
from langchain.agents import create_agent
from langgraph.checkpoint.memory import InMemorySaver
from langchain.tools import tool

load_dotenv()

products_stock = [
    {"product_id": "gas_001", "name": "Gas Propano", "price": 110, "stock": 3},
    {"product_id": "gas_002", "name": "Carton de huevos", "price": 19.99, "stock": 3},
]

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

agent = create_agent(
    model="gpt-4o-mini",
    system_prompt="You are a helpful sales agent assistant who helps customers with their inquiries about products and stock availability.",
    tools=[get_product_stock, sale_product],
    checkpointer=InMemorySaver()
)
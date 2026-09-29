"""El paquete del agente conversacional.

Corre como un proceso LangGraph aparte de FastAPI: comparte el mismo
repositorio y la misma base de datos, pero no el mismo proceso ni las
dependencias de `app.api`.  Nada bajo este paquete importa FastAPI.
"""

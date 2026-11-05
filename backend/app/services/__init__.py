"""Use-case services: coordinate the database, storage, and domain rules; own transactions.

Services know nothing about HTTP. They raise ServiceError with a stable `code`; the api layer
maps codes to HTTP status codes and the shared error response shape.
"""

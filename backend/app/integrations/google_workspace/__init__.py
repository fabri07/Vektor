"""Google Workspace integration — Decision B: local Python gateway.

Punto de entrada público:
    from app.integrations.google_workspace.gateway import GoogleWorkspaceGateway

El gateway es la única clase que AgentSupplier (y cualquier otro caller) debe instanciar.
GmailClient y TokenManager son detalles internos.
"""

"""
Service layer - all business logic lives here.

`app.py` imports ONLY from this package. If a Streamlit file ever needs to
`import db.database`, that is a sign the logic belongs in a service instead.
"""

__all__ = ["portfolio_service", "transaction_service", "analytics_service"]

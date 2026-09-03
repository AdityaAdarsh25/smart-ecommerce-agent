from backend.database import Base,engine

from backend.databases.merchant_db import merchant_db
from backend.databases.product_db import product_db
from backend.databases.mandate_db import mandate_db
from backend.databases.transaction_db import transaction_db
from backend.databases.buyer_db import buyer_db
from backend.databases.quote_db import quote_db
from backend.databases.approval_db import approval_db
from backend.databases.audit_db import audit_event_db
from backend.databases.mandate_db import (
    mandate_allowed_category_db,
    mandate_allowed_merchant_db,
)



def main() -> None:
    Base.metadata.create_all(bind=engine)
    print("All tables succesfully created")


if __name__ == "__main__":
    main()
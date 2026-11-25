"""Check that MongoDB Atlas is reachable using the credentials in .env.

Run:  conda run -n ml python test_connection.py

Prints only whether the connection worked (plus a hint on failure).
The connection string is never printed.
"""
import sys

from pymongo.errors import ConfigurationError, OperationFailure, ServerSelectionTimeoutError

from database import get_client


def main() -> int:
    try:
        client = get_client()
        client.admin.command("ping")
    except RuntimeError as error:  # MONGODB_URI missing
        print(f"MongoDB connection FAILED: {error}")
        return 1
    except ConfigurationError:
        print("MongoDB connection FAILED: the URI is malformed or the cluster's DNS name could not be resolved.")
        print("Fix: re-copy the connection string from Atlas (Connect > Drivers) and check your internet/DNS.")
        return 1
    except OperationFailure:
        print("MongoDB connection FAILED: authentication error.")
        print("Fix: check the database user's username/password in Atlas (Database Access) and in MONGODB_URI.")
        return 1
    except ServerSelectionTimeoutError:
        print("MongoDB connection FAILED: the cluster could not be reached within 10 seconds.")
        print("Fix: add your current IP address in Atlas (Network Access), and check firewall/VPN settings.")
        return 1

    print("MongoDB connection succeeded.")
    client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

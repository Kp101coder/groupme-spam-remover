from pathlib import Path
from key_helpers import generate_secret, persist_admin_key

ADMIN_KEY_FILE = Path('admin.key')

def main():
    secret = generate_secret(32)
    persist_admin_key('admin', secret, ADMIN_KEY_FILE)
    print("Admin key generated. Save this secret somewhere safe. It will not be shown again.")
    # Print the secret once to stdout so operator can copy it; do NOT log the secret.
    print(secret)

if __name__ == '__main__':
    main()

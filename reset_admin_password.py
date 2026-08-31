import getpass
from db import get_user_by_username, update_user
from auth import hash_password


def main():
    username = input("Admin username to reset: ").strip().lower()
    user = get_user_by_username(username)
    if not user:
        print("User not found.")
        return
    if user["role"] != "admin":
        print("This script is only for admin accounts. Use the admin panel to reset employee passwords.")
        return

    password = getpass.getpass("New password: ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Passwords do not match.")
        return

    update_user(str(user["_id"]), {
        "password_hash": hash_password(password),
        "must_change_password": False,
    })
    print(f"Password reset for admin '{username}'.")


if __name__ == "__main__":
    main()

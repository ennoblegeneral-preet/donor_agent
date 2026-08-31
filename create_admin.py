import getpass
from db import create_user, get_user_by_username
from auth import hash_password

def main():
    username = input("Admin username:").strip().lower()
    if get_user_by_username(username):
        print("User already exists")
        return

    password = getpass.getpass("Admin password:")
    confirm = getpass.getpass("Confirm password:")


    if password != confirm:
        print("Passwords do not match")
        return 
    create_user(username, hash_password(password), role="admin", must_change_password=False)
    print(f"Admin {username} created successfully")


if __name__ == "__main__":
    main()
    
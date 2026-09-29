from db import users_col, db

users_to_delete = ['']

# Delete users
result = users_col.delete_many({'username': {'$in': users_to_delete}})
print(f'✅ Deleted {result.deleted_count} users: {users_to_delete}')

# Also drop their company collections
for u in users_to_delete:
    col = f'companies_{u}'
    if col in db.list_collection_names():
        db.drop_collection(col)
        print(f'✅ Dropped company data: {col}')

print('✅ Admin preet is safe and untouched!')

# --- Admin deletion (separate list on purpose - more sensitive, so it's not
# accidentally triggered just by editing users_to_delete above) ---
admins_to_delete = []

if admins_to_delete:
    remaining_admins = users_col.count_documents({'role': 'admin', 'username': {'$nin': admins_to_delete}})
    if remaining_admins < 1:
        print(f'❌ Refusing to delete {admins_to_delete}: this would leave zero admin accounts.')
    else:
        admin_result = users_col.delete_many({'username': {'$in': admins_to_delete}, 'role': 'admin'})
        print(f'✅ Deleted {admin_result.deleted_count} admin(s): {admins_to_delete}')

        for u in admins_to_delete:
            col = f'companies_{u}'
            if col in db.list_collection_names():
                db.drop_collection(col)
                print(f'✅ Dropped company data: {col}')

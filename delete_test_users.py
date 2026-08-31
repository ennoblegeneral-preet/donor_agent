from db import users_col, db

users_to_delete = ['vijay', 'krishna', 'kavy']

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

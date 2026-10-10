# sql_to_csv.py
import sqlite3
import csv
import os

# Correctly locate the database file relative to this script
DB_FILE = 'musinsa_data.sqlite'
CSV_FILE = 'kream_cache.csv'

try:
    # Connect to the SQLite database
    conn = sqlite3.connect(DB_FILE)
    # Use a dictionary cursor to easily map column names
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Execute the query with a LEFT JOIN to include the product name
    query = """
    SELECT
        ksc.musinsa_goods_no,
        p.goodsName AS musinsa_goods_name,
        ksc.kream_product_id,
        ksc.kream_product_url,
        ksc.last_searched_at
    FROM
        kream_search_cache ksc
    LEFT JOIN
        products p ON ksc.musinsa_goods_no = p.goodsNo
    ORDER BY
        ksc.last_searched_at DESC
    """
    cursor.execute(query)

    # Fetch all rows
    rows = cursor.fetchall()

    # Get header from cursor description
    header = [description[0] for description in cursor.description]

    # Write to CSV file
    with open(CSV_FILE, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        # Write the header
        writer.writerow(header)
        # Write the data rows
        if rows:
            # writerows expects a list of sequences, not a list of sqlite3.Row objects directly
            writer.writerows([tuple(row) for row in rows])

    print(f"Data from 'kream_search_cache' with product names has been exported to '{CSV_FILE}'")

except sqlite3.Error as e:
    print(f"Database error: {e}")
except Exception as e:
    print(f"An error occurred: {e}")
finally:
    # Close the connection
    if 'conn' in locals() and conn:
        conn.close()
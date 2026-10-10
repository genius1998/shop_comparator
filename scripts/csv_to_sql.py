# csv_to_sql.py
import sqlite3
import csv

DB_FILE = 'musinsa_data.sqlite'
CSV_FILE = 'kream_cache.csv'

# Define the columns that exist in the kream_search_cache table
TABLE_COLUMNS = ['musinsa_goods_no', 'kream_product_id', 'kream_product_url']

def get_db_conn():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    return conn

try:
    # Open the CSV file and read the data
    with open(CSV_FILE, 'r', newline='', encoding='utf-8-sig') as f:
        reader = csv.reader(f)
        # Read the header row from the CSV
        csv_header = next(reader)
        
        # Find the indices of the columns we want to import
        col_indices = {col_name: i for i, col_name in enumerate(csv_header)}
        
        # Ensure all required columns are in the CSV
        # We will only use the columns that are present, so this check can be lenient
        columns_to_process = [col for col in TABLE_COLUMNS if col in col_indices]
        if not columns_to_process:
            print(f"Error: CSV file '{CSV_FILE}' contains none of the required columns: {TABLE_COLUMNS}")
            exit()

        # Get the indices for the columns in the correct order
        indices_to_keep = [col_indices[col] for col in columns_to_process]

        # Read the rest of the rows and filter them to only include the data for the columns we want
        filtered_rows = []
        for row in reader:
            if not any(row): # Skip empty rows
                continue
            # Safely extract data using the found indices
            filtered_row = tuple(row[i] if i < len(row) else None for i in indices_to_keep)
            filtered_rows.append(filtered_row)

    if not filtered_rows:
        print(f"'{CSV_FILE}' is empty or contains no valid data. No data to import.")
        exit()

    # Connect to the database
    with get_db_conn() as conn:
        cursor = conn.cursor()

        # Build the SQL statement with the correct columns
        placeholders = ', '.join(['?'] * len(columns_to_process))
        sql = f"INSERT OR REPLACE INTO kream_search_cache ({ ', '.join(columns_to_process) }) VALUES ({placeholders})"

        print(f"Inserting or replacing {len(filtered_rows)} rows into kream_search_cache...")
        # Execute the insert statement for all filtered rows
        cursor.executemany(sql, filtered_rows)
        
        # Commit the changes
        conn.commit()
        print(f"Successfully processed {cursor.rowcount} rows from '{CSV_FILE}' into 'kream_search_cache'.")

except FileNotFoundError:
    print(f"Error: '{CSV_FILE}' not found.")
except sqlite3.Error as e:
    print(f"Database error: {e}")
except Exception as e:
    print(f"An error occurred: {e}")
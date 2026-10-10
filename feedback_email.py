import psycopg2
import os
import re
import subprocess
import argparse
from datetime import datetime, timedelta, date

# --- Helper Functions (Defined first to avoid NameError) ---
def write_log(message, level="INFO"):
    """Writes a timestamped message to the console and the log file."""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    log_entry = f"[{timestamp}] [{level}] {message}"

    # Print to console (stdout)
    print(log_entry)

    # Write to log file
    try:
        with open(LOG_FILE, 'a') as f:
            f.write(log_entry + '\n')
    except IOError as e:
        print(f"[{timestamp}] [ERROR] Failed to write to log file {LOG_FILE}: {e}")

def read_file_content(file_path):
    """Reads content from a text file and returns it as a string."""
    try:
        if not os.path.exists(file_path):
            write_log(f"File not found: {file_path}", "ERROR")
            return None

        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except Exception as e:
        write_log(f"Error reading file {file_path}: {e}", "ERROR")
        return None

def add_to_exception_list(email, file_path):
    """
    Adds an email address to the exception list file with a timestamp.
    Format: email@example.com YYYY-MM-DD HH:MM:SS
    """
    try:
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        # Ensure directory exists
        os.makedirs(os.path.dirname(file_path), exist_ok=True)

        # Append email with timestamp to the exception list
        with open(file_path, 'a', encoding='utf-8') as f:
            f.write(f"{email.lower()} {timestamp}\n")

        write_log(f"Added {email} to exception list with timestamp {timestamp}", "INFO")
        return True
    except Exception as e:
        write_log(f"Error adding {email} to exception list: {e}", "ERROR")
        return False

def read_exception_list(file_path):
    """
    Reads email addresses from the exception list file and returns them as a set (case-insensitive).
    Format: email@example.com [optional_timestamp]
    - If timestamp is provided (YYYY-MM-DD HH:MM:SS), the entry expires after 6 months
    - If no timestamp, the entry is permanent
    """
    exception_emails = set()
    expired_count = 0
    permanent_count = 0
    temporary_count = 0

    try:
        if not os.path.exists(file_path):
            write_log(f"Exception list file not found: {file_path}. No emails will be excluded.", "WARN")
            return exception_emails

        current_time = datetime.now()
        six_months = timedelta(days=180)  # 6 months = 180 days

        with open(file_path, 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line or line.startswith('#'):  # Skip empty lines and comments
                    continue

                # Split line into email and optional timestamp
                parts = line.split(None, 1)  # Split on whitespace, max 2 parts
                if not parts:
                    continue

                email = parts[0].strip().lower()

                # Basic email validation
                if not email or '@' not in email:
                    write_log(f"Invalid email format on line {line_num}: {line}", "WARN")
                    continue

                # Check if timestamp is provided
                if len(parts) == 2:
                    timestamp_str = parts[1].strip()
                    try:
                        # Try to parse timestamp (supports various formats)
                        added_time = None
                        for fmt in ['%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d']:
                            try:
                                added_time = datetime.strptime(timestamp_str, fmt)
                                break
                            except ValueError:
                                continue

                        if added_time:
                            # Check if entry has expired (6 months old)
                            if current_time - added_time > six_months:
                                write_log(f"Expired exception entry (line {line_num}): {email} (added {timestamp_str})", "DEBUG")
                                expired_count += 1
                                continue
                            else:
                                exception_emails.add(email)
                                temporary_count += 1
                        else:
                            write_log(f"Invalid timestamp format on line {line_num}: {timestamp_str}. Treating as permanent.", "WARN")
                            exception_emails.add(email)
                            permanent_count += 1
                    except Exception as e:
                        write_log(f"Error parsing timestamp on line {line_num}: {e}. Treating as permanent.", "WARN")
                        exception_emails.add(email)
                        permanent_count += 1
                else:
                    # No timestamp = permanent exception
                    exception_emails.add(email)
                    permanent_count += 1

        write_log(f"Loaded {len(exception_emails)} email(s) from exception list: {permanent_count} permanent, {temporary_count} temporary, {expired_count} expired.", "INFO")
        return exception_emails
    except Exception as e:
        write_log(f"Error reading exception list file {file_path}: {e}", "ERROR")
        return exception_emails

def send_summary_email(sent_list, skipped_list, exception_list, duplicate_in_run_list):
    """
    Sends a summary email using sendmail with details of processed notifications.

    Args:
        sent_list: List of tuples (order_no, email, reason)
        skipped_list: List of tuples (order_no, email, reason)
        exception_list: List of tuples (order_no, email, reason)
        duplicate_in_run_list: List of tuples (order_no, email, reason)
    """
    recipient = "deven@staff.ownmail.com"
    subject = f"Feedback Email Notification Summary - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

    # Build email body
    body_lines = []
    body_lines.append("Feedback Email Notification Process Summary")
    body_lines.append("=" * 60)
    body_lines.append(f"Execution Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    body_lines.append("")

    # Sent emails section
    body_lines.append(f"NOTIFICATION REQUESTS QUEUED: {len(sent_list)}")
    body_lines.append("-" * 60)
    if sent_list:
        for order_no, email, details in sent_list:
            body_lines.append(f"  Order: {order_no}")
            body_lines.append(f"  Email: {email}")
            body_lines.append(f"  Details: {details}")
            body_lines.append("")
    else:
        body_lines.append("  No notification requests were queued.")
        body_lines.append("")

    # Exception list section
    body_lines.append(f"EXCLUDED (Exception List): {len(exception_list)}")
    body_lines.append("-" * 60)
    if exception_list:
        for order_no, email, reason in exception_list:
            body_lines.append(f"  Order: {order_no}")
            body_lines.append(f"  Email: {email}")
            body_lines.append(f"  Reason: {reason}")
            body_lines.append("")
    else:
        body_lines.append("  No emails were excluded.")
        body_lines.append("")

    # Skipped emails section
    body_lines.append(f"SKIPPED: {len(skipped_list)}")
    body_lines.append("-" * 60)
    if skipped_list:
        for order_no, email, reason in skipped_list:
            body_lines.append(f"  Order: {order_no}")
            body_lines.append(f"  Email: {email}")
            body_lines.append(f"  Reason: {reason}")
            body_lines.append("")
    else:
        body_lines.append("  No emails were skipped.")
        body_lines.append("")

    # Duplicate in run section
    body_lines.append(f"DUPLICATES IN CURRENT RUN: {len(duplicate_in_run_list)}")
    body_lines.append("-" * 60)
    if duplicate_in_run_list:
        for order_no, email, reason in duplicate_in_run_list:
            body_lines.append(f"  Order: {order_no}")
            body_lines.append(f"  Email: {email}")
            body_lines.append(f"  Reason: {reason}")
            body_lines.append("")
    else:
        body_lines.append("  No duplicate emails in this run.")
        body_lines.append("")

    # Summary
    body_lines.append("=" * 60)
    body_lines.append(f"TOTAL PROCESSED: {len(sent_list) + len(skipped_list) + len(exception_list) + len(duplicate_in_run_list)}")
    body_lines.append(f"  - Queued: {len(sent_list)}")
    body_lines.append(f"  - Excluded: {len(exception_list)}")
    body_lines.append(f"  - Skipped: {len(skipped_list)}")
    body_lines.append(f"  - Duplicates in Run: {len(duplicate_in_run_list)}")

    body = "\n".join(body_lines)

    # Prepare email message for sendmail
    email_message = f"To: {recipient}\n"
    email_message += f"Subject: {subject}\n"
    email_message += "Content-Type: text/plain; charset=UTF-8\n"
    email_message += "\n"
    email_message += body

    if DRY_RUN:
        write_log("DRY RUN: Summary email not sent. See logs for details.", "INFO")
        return

    # Send email using sendmail
    try:
        process = subprocess.Popen(
            ['/usr/sbin/sendmail', '-t', '-oi'],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        stdout, stderr = process.communicate(email_message.encode('utf-8'))

        if process.returncode == 0:
            write_log(f"Summary email sent successfully to {recipient}", "INFO")
        else:
            write_log(f"Failed to send summary email. Return code: {process.returncode}, Error: {stderr.decode('utf-8')}", "ERROR")
    except Exception as e:
        write_log(f"Error sending summary email: {e}", "ERROR")

# --- Configuration ---
# NOTE: Replace these placeholders with your actual database connection details.
DB_NAME = os.environ.get("DB_NAME", "e2fax")
DB_USER = os.environ.get("DB_USER", "domains")
DB_HOST = os.environ.get("DB_HOST", "localhost")
# DB_PASS and DB_PORT removed as per requirement

# Define the path for the log file
LOG_FILE = '/var/log/ownmail/feedback_notification.log'

# Define the path for the exception list file
EXCEPTION_LIST_FILE = '/var/log/ownmail/feedback_email_exception.txt'

# Define constant values for the notification table array columns
NOTIFICATION_TYPE = ['email']

# Fallback templates if database fetch fails
FALLBACK_SUBJECT_TEMPLATE = "Rate TSIM in 30 seconds?"
FALLBACK_MESSAGE_TEMPLATE = """Hi <customer_name>,

Was TSIM helpful on your trip?

Please rate us on the App Store / Play Store. It takes 30 seconds and really helps us.

<a href="<review_link>">Leave your review</a>

Team TSIM"""

FALLBACK_NON_APP_SUBJECT = "How was your trip with TSIM?"
FALLBACK_NON_APP_MESSAGE = """Hi <customer_name>,

Was TSIM helpful on your trip? We'd love to hear how it went. Reply to this email to share your feedback.

For your next trip, try the TSIM app. Download it from the <a href="<review_link>">App Store / Play Store</a>.

Team TSIM"""
FALLBACK_PUSH_SUBJECT = "Rate TSIM in 30 seconds?"
FALLBACK_PUSH_MESSAGE = "Was TSIM helpful on your trip? Please rate TSIM in the App Store or Play Store."

def get_optional_template(key, fallback):
    rows = queryDB("SELECT value FROM config_values WHERE key = %s", (key,))
    return rows[0][0] if rows and rows[0][0] else fallback

# Database queries for fetching templates
SUBJECT_TEMPLATE_QUERY = "SELECT value FROM config_values WHERE key = 'feedback_email_subject_v2'"
MESSAGE_TEMPLATE_QUERY = "SELECT value FROM config_values WHERE key = 'feedback_email_message_v2'"

# Fetch templates from database
def get_templates_from_db():
    """Fetches email subject and message templates from the database."""
    subject_result = queryDB(SUBJECT_TEMPLATE_QUERY)
    message_result = queryDB(MESSAGE_TEMPLATE_QUERY)

    subject_template = None
    message_template = None

    if subject_result and subject_result[0] and subject_result[0][0]:
        subject_template = subject_result[0][0]
        write_log(f"Fetched subject template from database: {subject_template}", "DEBUG")
    else:
        write_log("Failed to fetch subject template from database, using fallback", "WARN")
        subject_template = FALLBACK_SUBJECT_TEMPLATE

    if message_result and message_result[0] and message_result[0][0]:
        message_template = message_result[0][0]
        write_log(f"Fetched message template from database", "DEBUG")
    else:
        write_log("Failed to fetch message template from database, using fallback", "WARN")
        message_template = FALLBACK_MESSAGE_TEMPLATE

    return subject_template, message_template

# Templates will be fetched during process execution
RAW_MESSAGE_TEMPLATE = None
RAW_SUBJECT_TEMPLATE = None

STATUS_EMAIL = ['email_pending'] # Status before sending
PRIORITY = 'normal'

# Updated Default Email
DEFAULT_FROM_EMAIL = 'services@tsim.mobi'
DEFAULT_VALIDITY_DAYS = 30
# Dry run mode: if true, do not write to DB/exception list or send email
DRY_RUN = False
# --- End Configuration ---

def queryDB(query, params=None, setUTF=False):
    """
    Executes a query using specific hardcoded connection details.
    """
    conn = None
    cursor = None
    rows = None

    try:
        # Use hardcoded connection details as requested
        conn = psycopg2.connect(database="e2fax", user="domains", host="localhost")
        conn.set_client_encoding('UTF8')
        cursor = conn.cursor()

        # Using write_log to match the script's logging style
        write_log(f"Executing query: {query} with parameters: {params}", "DEBUG")

        # Execute the query with parameters
        cursor.execute(query, params)

        # Fetch all rows only if it is a SELECT query
        if query.strip().lower().startswith("select"):
            rows = cursor.fetchall()

        # Commit the changes for INSERT, UPDATE, DELETE queries (and SELECT as per provided snippet)
        conn.commit()

        # Log the number of affected rows if it is an INSERT, UPDATE, or DELETE query
        if query.strip().lower().startswith(("insert", "update", "delete")):
            write_log(f"{cursor.rowcount} rows affected.", "DEBUG")
            rows = cursor.rowcount

    except Exception as e:
        write_log(f"Error executing query: {e}", "ERROR")

    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

    return rows

# All plans require a usage record updated within the existing two-month window.
# Daily (gbpd) and unlimited (unl/unlimited) SKUs bypass the consumption test,
# including zero totals. Fixed bundles require >=35% consumption.
# Literal SQL percent signs are doubled because this query takes parameters.
CUSTOMER_SELECT_QUERY = """
SELECT
    Q1.customer_email,
    Q1.simno,
    Q1.order_no,
    Q1.username,
    Q1.domainname,
    Q1.from_date,
    Q1.country,
    Q1.clientname,
    Q1.is_app_user,
    Q1.usage_filter_bypassed
FROM
    (
        SELECT
            COALESCE(ct.emailadd, su.emailadd) AS customer_email,
            ct.simno,
            ct.order_no,
            ct.username,
            ct.domainname,
            ct.from_date,
            ct.country,
            CASE
                WHEN COALESCE(NULLIF(ct.clientname, 'empty'), su.clientname) LIKE 'amazon%%' THEN ''
                ELSE COALESCE(NULLIF(ct.clientname, 'empty'), su.clientname)
            END AS clientname,
            EXISTS (SELECT 1 FROM ta_notification_id ni WHERE ni.username = ct.username) AS is_app_user,
            (COALESCE(ct.country, '') ~* '(gbpd|unl|unlimited)') AS usage_filter_bypassed
        FROM
            sim_stock_trip ct
        JOIN
            sim_user su ON ct.username = su.username
        WHERE
            EXISTS (
                SELECT 1 FROM ta_esim_usage_v2 usage
                WHERE usage.iccid = ct.simno
                    AND usage.last_updated > NOW() - INTERVAL '2 months'
                    AND (
                        COALESCE(ct.country, '') ~* '(gbpd|unl|unlimited)'
                        OR (usage.remaining <= (usage.total * 0.65) AND usage.total > 0)
                    )
            )

            AND COALESCE(ct.emailadd, su.emailadd) != 'japan001@tsim.in'
    ) AS Q1
WHERE
    NOT EXISTS (
        SELECT 1
        FROM tsim_notification TN
        WHERE
            LOWER(TRIM(TN.email)) = LOWER(TRIM(Q1.customer_email))
            AND (TN.subject_template[1] LIKE 'Hope You Had a Great Trip%%' OR TN.subject_template[1] LIKE '%%Rate TSIM%%' OR TN.subject_template[1] = 'How was your trip with TSIM?' OR TN.subject_template[1] = ANY(%s))
    )

    AND NOT EXISTS (
        SELECT 1
        FROM sim_stock_trip ct_prev
        JOIN tsim_notification TN ON TN.order_no = ct_prev.order_no
        WHERE
            ct_prev.username = Q1.username
            AND ct_prev.country = Q1.country
            AND ct_prev.order_no != Q1.order_no  -- Different order (previous order)
            AND LOWER(TRIM(TN.email)) = LOWER(TRIM(Q1.customer_email))
            AND (TN.subject_template[1] LIKE 'Hope You Had a Great Trip%%' OR TN.subject_template[1] LIKE '%%Rate TSIM%%' OR TN.subject_template[1] = 'How was your trip with TSIM?' OR TN.subject_template[1] = ANY(%s))
    );
"""

# The INSERT query template for the tsim_notification table
NOTIFICATION_INSERT_QUERY = """
INSERT INTO tsim_notification
    (username, order_no, email,
     notification_type, subject_template,
     message_template, subject_content, message_content, status, priority, from_email)
VALUES
    (%(username)s, %(order_no)s, %(email)s,
     %(notification_type)s, %(subject_template)s,
     %(message_template)s, %(subject_content)s, %(message_content)s, %(status)s, %(priority)s, %(from_email)s);
"""

VALIDITY_LOOKUP_QUERY = "SELECT validity FROM ta_plans_v4_in_children WHERE sku = %s"

def get_from_email(domain):
    """Determines the correct 'From' email address based on the domain."""
    if domain == 'tsim.in':
        return 'services@tsim.in'
    elif domain == 'tsim.mobi':
        return 'services@tsim.mobi'
    else:
        return DEFAULT_FROM_EMAIL

def get_review_link(domain):
    """Determines the correct review link based on the domain."""
    if domain == 'tsim.in':
        return 'https://mail.tsim.in/review.html'
    elif domain == 'tsim.mobi':
        return 'https://mail.tsim.mobi/review.html'
    else:
        return 'https://mail.tsim.mobi/review.html'

def get_validity_days(sku):
    """
    Extracts validity days from SKU string using regex.
    If regex fails, queries the database using queryDB.
    """
    if not sku:
        return DEFAULT_VALIDITY_DAYS

    # 1. Regex Extraction
    # Priority 1: Patterns with 'D' or 'd' (e.g., 30D, 3d, 10Days)
    days_match = re.search(r'(\d+)[dD](?:ays?)?', sku)
    if days_match:
        return int(days_match.group(1))

    # Priority 2: Patterns with delimiters like underscore or hyphen (e.g., _30_, -30-)
    underscore_match = re.search(r'_(\d+)_', sku)
    if underscore_match:
        return int(underscore_match.group(1))

    hyphen_match = re.search(r'-(\d+)-', sku)
    if hyphen_match:
        return int(hyphen_match.group(1))

    # 2. Database Lookup via queryDB
    try:
        result_rows = queryDB(VALIDITY_LOOKUP_QUERY, (sku,))

        if result_rows and result_rows[0] and result_rows[0][0]:
            db_validity = str(result_rows[0][0])
            db_match = re.search(r'(\d+)', db_validity)
            if db_match:
                return int(db_match.group(1))
    except Exception as e:
        write_log(f"Error fetching validity for SKU {sku}: {e}", "WARN")

    # 3. Default Fallback
    write_log(f"Could not determine validity for SKU '{sku}'. Using default {DEFAULT_VALIDITY_DAYS} days.", "WARN")
    return DEFAULT_VALIDITY_DAYS

def process_notifications():
    """Connects to DB, fetches target users, and inserts notification records."""
    write_log("Starting customer feedback notification process.")
    if DRY_RUN:
        write_log("DRY RUN ENABLED: No database writes, exception list updates, or summary email will be sent.", "INFO")

    # Load exception list
    exception_emails = read_exception_list(EXCEPTION_LIST_FILE)

    # Fetch templates from database
    global RAW_SUBJECT_TEMPLATE, RAW_MESSAGE_TEMPLATE
    RAW_SUBJECT_TEMPLATE, RAW_MESSAGE_TEMPLATE = get_templates_from_db()

    non_app_subject = get_optional_template('feedback_email_non_app_subject', FALLBACK_NON_APP_SUBJECT)
    non_app_message = get_optional_template('feedback_email_non_app_message', FALLBACK_NON_APP_MESSAGE)
    push_subject = get_optional_template('feedback_push_subject', FALLBACK_PUSH_SUBJECT)
    push_message = get_optional_template('feedback_push_message', FALLBACK_PUSH_MESSAGE)

    # Templates will always be available now (either from DB or fallback)
    write_log(f"Using subject: {RAW_SUBJECT_TEMPLATE[:50]}...", "INFO")
    write_log(f"Using message template (first 50 chars): {RAW_MESSAGE_TEMPLATE[:50]}...", "INFO")

    try:
        write_log("Executing customer selection query...")
        write_log(f"Subject template for matching: '{RAW_SUBJECT_TEMPLATE}'", "DEBUG")
        write_log(f"Subject template length: {len(RAW_SUBJECT_TEMPLATE)}", "DEBUG")

        # 1. Execute the SELECT query
        feedback_subjects = [RAW_SUBJECT_TEMPLATE, non_app_subject]
        customers_to_notify = queryDB(CUSTOMER_SELECT_QUERY, (feedback_subjects, feedback_subjects))

        if not customers_to_notify:
            write_log("No new customers needing feedback notifications found, or DB error occurred.")
            return

        write_log(f"Found {len(customers_to_notify)} unique customers potential candidates.")

        insertion_count = 0
        skipped_count = 0
        exception_count = 0
        duplicate_in_run_count = 0

        # Lists to track details for summary email
        sent_list = []
        skipped_list = []
        exception_list = []
        duplicate_in_run_list = []

        # Map to track emails already processed in this run (prevents duplicates)
        processed_emails = set()

        # 2. Iterate through results and prepare insertion data
        for row in customers_to_notify:
            # Unpack fields - country is the SKU
            customer_email, simno, order_no, username, domainname, from_date, country, clientname, is_app_user, usage_filter_bypassed = row

            # Check if email has already been processed in this run
            email_key = customer_email.strip().lower() if customer_email else None
            if email_key and email_key in processed_emails:
                write_log(f"Skipping Order {order_no} (Email: {customer_email}): Email already processed in this run.", "INFO")
                duplicate_in_run_count += 1
                duplicate_in_run_list.append((order_no, customer_email, "Email already processed in current run"))
                continue

            # Check if email is in exception list
            if customer_email and customer_email.strip().lower() in exception_emails:
                write_log(f"Skipping Order {order_no} (Email: {customer_email}): Email is in exception list.", "INFO")
                exception_count += 1
                exception_list.append((order_no, customer_email, "Email in exception list"))
                continue

            # Skip emails containing "marketplace.amazon"
            if customer_email and "marketplace.amazon" in customer_email.lower():
                write_log(f"Skipping Order {order_no} (Email: {customer_email}): Contains 'marketplace.amazon'.", "INFO")
                skipped_count += 1
                skipped_list.append((order_no, customer_email, "Email contains 'marketplace.amazon'"))
                continue

            # Sanitize clientname
            if clientname is None or clientname == 'None' or clientname == 'none':
                clientname = ''

            # --- DATE LOGIC START ---
            if not from_date:
                write_log(f"Skipping Order {order_no} (Email: {customer_email}): No start date (from_date) found.", "WARN")
                skipped_count += 1
                skipped_list.append((order_no, customer_email, "No start date (from_date) found"))
                continue

            # Calculate Validity
            validity_days = get_validity_days(country)

            # Ensure from_date is a date object
            if isinstance(from_date, datetime):
                start_date = from_date.date()
            elif isinstance(from_date, date):
                start_date = from_date
            else:
                try:
                    start_date = datetime.strptime(str(from_date), '%Y-%m-%d').date()
                except ValueError:
                    write_log(f"Skipping Order {order_no} (Email: {customer_email}): Invalid date format {from_date}", "ERROR")
                    skipped_count += 1
                    skipped_list.append((order_no, customer_email, f"Invalid date format: {from_date}"))
                    continue

            # Calculate Trip End and Notification Trigger Date
            trip_end_date = start_date + timedelta(days=validity_days)
            notification_trigger_date = trip_end_date + timedelta(days=2)
            today = datetime.now().date()

            # Check if we should send the notification
            if today < notification_trigger_date:
                write_log(f"Skipping Order {order_no} (Email: {customer_email}): Too early - trigger date is {notification_trigger_date}, today is {today}", "INFO")
                skipped_count += 1
                skipped_list.append((order_no, customer_email, f"Too early - trigger date is {notification_trigger_date}"))
                continue
            # --- DATE LOGIC END ---

            # Dynamically set FROM_EMAIL based on domainname
            current_from_email = get_from_email(domainname)

            # Dynamically set review link based on domainname
            review_link = get_review_link(domainname)

            # Construct Dynamic Message Content
            dynamic_content_str = f"customer_name:{clientname}|||review_link:{review_link}"

            # Data dictionary for parameterized INSERT query
            insert_data = {
                'username': username,
                'order_no': order_no,
                'email': customer_email,
                'notification_type': list(NOTIFICATION_TYPE),
                'subject_template': [RAW_SUBJECT_TEMPLATE if is_app_user else non_app_subject],
                'message_template': [RAW_MESSAGE_TEMPLATE if is_app_user else non_app_message],
                'subject_content': [dynamic_content_str],
                'message_content': [dynamic_content_str],   # Wrapped in list for text[]
                'status': list(STATUS_EMAIL),
                'priority': PRIORITY,
                'from_email': current_from_email
            }

            if is_app_user:
                insert_data['notification_type'].append('push')
                insert_data['subject_template'].append(push_subject)
                insert_data['message_template'].append(push_message)
                insert_data['subject_content'].append(dynamic_content_str)
                insert_data['message_content'].append(dynamic_content_str)
                insert_data['status'].append('push_pending')

            details = (f"Channels: {', '.join(insert_data['notification_type'])}, From: {current_from_email}, "
                       f"Validity: {validity_days} days, Country/SKU: {country}, "
                       f"Usage rule: {'daily/unlimited bypass' if usage_filter_bypassed else 'fixed plan >=35% consumed'}")
            if DRY_RUN:
                write_log(f"DRY RUN templates: {insert_data['subject_template']!r}; "
                          f"bodies: {insert_data['message_template']!r}; placeholders: {dynamic_content_str!r}")
                insertion_count += 1
                if email_key:
                    processed_emails.add(email_key)
                sent_list.append((order_no, customer_email, details))
                write_log(f"DRY RUN: Would queue notification for Order: {order_no}, Email: {customer_email}, {details}")
            else:
                # 3. Execute the INSERT query for each customer using queryDB
                if queryDB(NOTIFICATION_INSERT_QUERY, insert_data) != 1:
                    skipped_count += 1
                    skipped_list.append((order_no, customer_email, "Notification queue insert failed"))
                    continue
                insertion_count += 1

                # Mark this email as processed in this run
                if email_key:
                    processed_emails.add(email_key)

                # Add email to exception list with timestamp
                add_to_exception_list(customer_email, EXCEPTION_LIST_FILE)

                sent_list.append((order_no, customer_email, details))

                write_log(f"Queued notification record for Order: {order_no}, Email: {customer_email}, {details}")

        queue_label = 'would queue' if DRY_RUN else 'queued'
        write_log(f"\nSummary: {insertion_count} {queue_label}, {skipped_count} skipped, {exception_count} excluded (exception list), {duplicate_in_run_count} duplicates in current run.")

        # Send summary email
        send_summary_email(sent_list, skipped_list, exception_list, duplicate_in_run_list)

    except Exception as e:
        write_log(f"An unexpected error occurred in main process: {e}", "FATAL")
    finally:
        write_log("Notification process finished.")

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Send feedback email notifications.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not write to DB/exception list or send summary email; log what would happen."
    )
    args = parser.parse_args()

    DRY_RUN = args.dry_run
    process_notifications()

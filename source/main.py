import os, sys
import re
import logging

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from collector import collect_youtube_comments, YouTubeQuotaError
from config.EN_short_text import COMMON_SHORT_EN_WORDS, COMMON_EN_ABBREVIATIONS_ACRONYMS

logging.basicConfig(level = logging.INFO, format = "%(asctime)s - %(levelname)s - %(message)s")

def load_api_key(filepath):
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"API key file not found: {filepath}")
    with open(filepath, "r") as file:
        for line in file:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("YOUTUBE_API_KEY"):
                return line.split("=", 1)[1].strip()
    
    raise ValueError("Missing YouTube API key. Please check the api_key.txt file in the config directory.")

API_KEY = load_api_key("config/api_key.txt")

ORDER_MAP = {"r": "Relevance", "t": "Time", "o": "Time_Oldest"}

 # First operation: Define the unit of analysis
def read_video_ids(filepath):
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"File not found: {filepath}")
    
    YOUTUBE_ID_PATTERN = re.compile(r'^[a-zA-Z0-9_-]{11}$')
    
    valid_ids = []
    invalid_ids_count = 0
    
    with open(filepath, "r") as file:
        for line in file:
            raw_id = line.split("#")[0].strip()
            
            if not raw_id:
                continue
            
            if YOUTUBE_ID_PATTERN.match(raw_id):
                valid_ids.append(raw_id)
            else:
                logging.warning(f"ID ignored (invalid format): '{raw_id}'. Must be an 11-character ID.")
                invalid_ids_count += 1
    
    if not valid_ids:
        raise ValueError(f"No valid video ID found in {filepath}")

    if invalid_ids_count > 0:
        logging.warning(f"\nWARNING: {invalid_ids_count} invalid ID(s) were ignored before data collection.")
        
    return valid_ids


def get_user_input():
    max_retries = 3
    for attempt in range(max_retries):
        try:
            max_comments = int(input("How many top-level comments do you want to retrieve per video? (maximum 2000): ").strip())
            if 1 <= max_comments <= 2000:
                break
            else:
                print("\nEnter a number between 1 and 2000.")
        except ValueError:
            print("\nEnter a valid integer.")
    else:
        max_comments = 100
        print("\nDefault value of 100 used for top-level comments.")

    while True:
        comment_order_input = input("\nSelect comment order by pressing the associated key:\n" 
                                    "\n'r' -> Relevance. Please note that the API may return an order that differs from the one displayed on the platform.\n"
                                    "\n't' -> Time. Retrieve top-level comments from newest to oldest.\n"
                                    "\n'o' -> Time_Oldest. Retrieve top-level comments from oldest to newest. Requires full pagination, which may consume a lot of quota on videos with many comments.\n"
                                    "\n > ").strip().lower()

        if comment_order_input in ORDER_MAP:
            comment_order = ORDER_MAP[comment_order_input]
            break
        else:
            print("\nInvalid input. Please try again.")

    while True:
        collect_replies_user_input = input("\nDo you want to collect comment replies? (yes/no): ").strip().lower()

        if collect_replies_user_input in {'yes', 'no'}:
            collect_replies = collect_replies_user_input == 'yes'
            break
        else:
            print("\nPlease answer 'yes' or 'no'.")      

    max_replies = 0
    if collect_replies:
        for attempt in range(max_retries):
            try:
                max_replies = int(input("\nHow many replies do you want to retrieve per comment? (maximum 50): ").strip())
                if 1 <= max_replies <= 50:
                    break
                else:
                    print("\nEnter a number between 1 and 50.")
            except ValueError:
                print("\nEnter a valid integer.")
        else:
            max_replies = 10
            print("\nDefault value of 10 used for comment replies.")

    while True:
        language_filter_input = input("\nDo you want to apply the language-filtering pipeline on raw text (i.e., remove emojis, normalize punctuation, and collect only English-written comments)? (yes/no): ").strip().lower()

        if language_filter_input in {'yes', 'no'}:
            language_filter = language_filter_input == 'yes'
            break
        else:
            print("\nPlease answer 'yes' or 'no'.")

    while True:
        output_filename = input("\nOutput file name (file_name.csv): ").strip()

        if not output_filename.endswith(".csv"):
            output_filename += ".csv"

        if re.match(r'^[\w,\s-]+\.csv$', output_filename):
            break
        else:
            print("Invalid file name.")
    
    return collect_replies, max_comments, max_replies, output_filename, language_filter, comment_order


if __name__ == "__main__":

    print("\n\n-- YouTube Data Collector - Configuration --\n")
    
    video_ids_file = "source/video_ids.txt"

    try:
        video_ids = read_video_ids(video_ids_file)
        print(f"{len(video_ids)} valid video(s) found in {video_ids_file}\n")
    except (FileNotFoundError, ValueError) as e:
        logging.error(str(e))
        raise
    
    collect_replies, max_comments, max_replies, output_filename, language_filter, comment_order = get_user_input()
    
    print("\n\n-- Start of data collection --\n")
    
    try:
        df_comments, language_filter_report = collect_youtube_comments(
            api_key = API_KEY,
            video_ids = video_ids,
            max_comments_per_video = max_comments,
            max_replies_per_comment = max_replies,
            collect_replies = collect_replies,
            language_filter = language_filter,
            comment_order = comment_order)

        if not df_comments.empty:
            total_comments = len(df_comments[df_comments['is_reply'] == False])
            total_replies = len(df_comments[df_comments['is_reply'] == True])
            
            print(f"\n{'_' * 45}\n"
                "\nDATA COLLECTION SUMMARY:\n"
                f"\n  - Comment order selected -> {comment_order}\n"
                f"\n  - Total top-level comments -> {total_comments}\n"
                f"\n  - Total replies -> {total_replies}\n"
                f"{'_' * 45}\n")
        
        else:
            print("\nNo data retrieved. Check per-video logs or API status (e.g., commentsDisabled).\n")

        if language_filter and not df_comments.empty:
            print(
                f"LANGUAGE FILTERING SUMMARY:\n\n"
                f"Initial number of retrieved comments -> {language_filter_report['initial_count']}\n"
                f"\n  - Comments kept after filtering -> {language_filter_report['kept_count']}\n"
                f"\n  - Comments excluded -> {language_filter_report['excluded_count']}\n"
                f"\nReasons for exclusion:\n"
                f"\n - Empty after preprocessing (emojis and white-space) -> {language_filter_report['because_empty']}\n"
                f"\n - Non-English content detected (too likely being another language than EN) -> {language_filter_report['because_non_english']}\n"
                f"\n - Language detection failed (text too short or ambiguous to be detected) -> {language_filter_report['because_langdetect_failed']}\n"
                f"\n - Others (e.g., API error, max_results reached, exception raised) -> {language_filter_report['others']}\n"
                f"{'_' * 45}\n")

        if not df_comments.empty:
            output_dir = "data"
            os.makedirs(output_dir, exist_ok = True)

            output_path = os.path.join(output_dir, output_filename)
            df_comments.to_csv(output_path, index = False, sep = ";", encoding = "utf-8-sig")

            print("-- Data collection process completed --\n")
        else:          
            output_path = os.path.join("data", output_filename)
            try:
                if os.path.exists(output_path) and os.path.getsize(output_path) == 0:
                    os.remove(output_path)
            except Exception:
                pass
        
    except YouTubeQuotaError as e:
        logging.critical(f"CRITICAL STOP. {str(e)}")
    
    except Exception as e:
        logging.error(f"Unexpected fatal error during data collection: {e}", exc_info = True)
import time
import logging
import re
import string
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import pandas as pd
from tqdm import tqdm
from langdetect import detect_langs, DetectorFactory, LangDetectException
from collections import deque

 # Text preprocessing for language detection and filtering

DetectorFactory.seed = 1
from config.emoji_filter import remove_emojis
from config.EN_short_text import COMMON_SHORT_EN_WORDS, COMMON_EN_ABBREVIATIONS_ACRONYMS

TOKEN_PATTERN = re.compile(r"\b\w+\b", flags = re.UNICODE)
PUNCT_TABLE = str.maketrans("", "", string.punctuation)

def clean_text(text):
    if not isinstance(text, str):
        return ""

    text = remove_emojis(text)
    text = text.translate(PUNCT_TABLE)
    text = re.sub(r"\s+", " ", text).strip()

    return text

def is_english_short_text(text):
    tokens = text.lower().split()

    if not tokens:
        return False
    if not all(token.isascii() for token in tokens):
        return False

    has_common_word = any(token in COMMON_SHORT_EN_WORDS for token in tokens)
    has_abbrev = any(token in COMMON_EN_ABBREVIATIONS_ACRONYMS for token in tokens)

    return has_common_word or (has_abbrev and len(tokens) >= 2)

 # First operation: Manage API constraints and quota limits

class YouTubeQuotaError(Exception):
    pass

def execute_with_retries(request, max_retries = 5):
    retries = 0
    while retries < max_retries:
        try:
            return request.execute()
        except HttpError as e:
            error_content = e.content.decode('utf-8')

            if 'rateLimitExceeded' in error_content or 'userRateLimitExceeded' in error_content:
                retries += 1
                wait_time = min(2 ** retries, 64)
                logging.warning(f"Rate limit exceeded. Retrying in {wait_time} seconds")
                time.sleep(wait_time)

            elif 'quotaExceeded' in error_content:
                logging.error("API quota exceeded")
                raise YouTubeQuotaError("API quota exceeded")

            else:
                raise e

    raise YouTubeQuotaError(f"Maximum retry attempts reached ({max_retries}). ""Last error was not recoverable.")

 # Second operation: Manage YouTube API HTTP errors

def handle_youtube_http_error(error, *, video_id = None, parent_id = None):
    error_content = error.content.decode("utf-8", errors = "ignore")

    context = []
    if video_id:
        context.append(f"video = {video_id}")
    if parent_id:
        context.append(f"comment = {parent_id}")

    prefix = f"[{' | '.join(context)}] " if context else ""

    if 'commentsDisabled' in error_content or 'disabled comments' in error_content.lower():
        logging.warning(f"{prefix}Comments are disabled.")
        return "IGNORED"

    if error.resp.status == 404 or 'videoNotFound' in error_content:
        logging.warning(f"{prefix}Video not found or private.")
        return "IGNORED"

    if error.resp.status == 403:
        logging.warning(f"{prefix}Access denied (403 error).")
        return "IGNORED"

    logging.error(f"{prefix}Unhandled YouTube API error: {error}")
    raise error

 # Third operation: data collection

def data_metadata_to_retrieve(item, video_id):
    snippet = item["snippet"]["topLevelComment"]["snippet"]

    return {
        "video_id": video_id,
        "comment_id": item["id"],
        "author": snippet.get("authorDisplayName"),
        "author_id": snippet.get("authorChannelId", {}).get("value", "Not available"),
        "text": snippet.get("textDisplay", ""),
        "like_count": snippet.get("likeCount"),
        "published_at": snippet.get("publishedAt"),
        "updated_at": snippet.get("updatedAt"),
        "is_reply": False}

def fetch_paginated_results(service, request, max_results = None):

    results = []

    while request:
        response = execute_with_retries(request)
        items = response.get("items", [])
        results.extend(items)

        if max_results and len(results) >= max_results:
            break

        request = service.list_next(request, response)

    return results[:max_results] if max_results else results

def get_video_comments(youtube, video_id, max_results = None, order = 'relevance', language_filter = True):
    
    comment_threads = youtube.commentThreads()

    request = comment_threads.list(
        part = 'snippet',
        videoId = video_id,
        maxResults = 100,
        textFormat = 'plainText',
        order = "time" if order.lower() == "time_oldest" else order.lower())
 
    kept_comments = []
    page_reports = []

    # time_oldest retrieval mode
    if order.lower() == "time_oldest":
        buffer = deque(maxlen = max_results if max_results else None)

        while request:
            response = execute_with_retries(request)
            items = response.get("items", [])
            page_rows = [data_metadata_to_retrieve(i, video_id) for i in items]

            page_df = pd.DataFrame(page_rows)
            if language_filter:
                page_df, report = clean_dataframe(page_df, True)
                page_reports.append(report)

            for row in page_df.to_dict(orient = "records"):
                buffer.append(row)
            
            request = comment_threads.list_next(request, response)
        
        return list(reversed(buffer)), page_reports

    # time and relevance retrieval orders
    while request:
        response = execute_with_retries(request)
        items = response.get("items", [])
        page_rows = [data_metadata_to_retrieve(i, video_id) for i in items]

        page_df = pd.DataFrame(page_rows)
        if language_filter:
            page_df, report = clean_dataframe(page_df, True)
            page_reports.append(report)

        kept_comments.extend(page_df.to_dict(orient = "records"))

        if max_results and len(kept_comments) >= max_results:
            break

        request = comment_threads.list_next(request, response)

    return kept_comments[:max_results] if max_results else kept_comments, page_reports

def get_replies_to_comments(youtube, parent_comments, max_replies = None):

    all_replies = []

    parent_data = [(p.get('comment_id'), p.get('video_id')) for p in parent_comments]

    for parent_id, video_id in parent_data:
        request = youtube.comments().list(
            part = 'snippet',
            parentId = parent_id,
            maxResults = 100,
            textFormat = 'plainText')

        replies_for_parent = []

        while request:
            try:
                response = execute_with_retries(request)
            except HttpError as e:
                if handle_youtube_http_error(e, video_id = video_id, parent_id = parent_id) == "IGNORED":
                    break
                raise

            items = response.get("items", [])

            for item in items:
                snippet = item.get('snippet', {})
                replies_for_parent.append({
                    'video_id': video_id,
                    'comment_id': item.get('id'),
                    'parent_id': parent_id,
                    'author': snippet.get('authorDisplayName'),
                    'author_id': snippet.get('authorChannelId', {}).get('value', 'Not available'),
                    'text': snippet.get('textDisplay', ''),
                    'like_count': snippet.get('likeCount'),
                    'published_at': snippet.get('publishedAt'),
                    'updated_at': snippet.get('updatedAt'),
                    'is_reply': True})

            request = youtube.comments().list_next(request, response)

            if max_replies and len(replies_for_parent) >= max_replies:
                break

        if max_replies and len(replies_for_parent) > max_replies:
            replies_for_parent = replies_for_parent[:max_replies]

        all_replies.extend(replies_for_parent)

    return all_replies

 # Fourth operation: structure raw data

def clean_dataframe(df, language_filter = True):

    report = {
        "initial_count": len(df),
        "kept_count": 0,
        "excluded_count": 0,
        "because_empty": 0,
        "because_non_english": 0,
        "because_langdetect_failed": 0}

    if df.empty:
        return df, report

    if not language_filter:
        df["text"] = (df["text"].astype(str).str.replace(r"\r\n|\r|\n", " ", regex = True).str.strip())
        report["kept_count"] = len(df)
        return df.reset_index(drop = True), report    

    df["text"] = (df["text"].astype(str).str.replace(r"\r\n|\r|\n", " ", regex = True).str.strip())
    df["text"] = df["text"].apply(clean_text)

    empty_mask = ~df["text"].str.strip().astype(bool)
    report['because_empty'] = int(empty_mask.sum())
    df = df[~empty_mask]

    lang_results = []
    for text in df["text"]:
        try:
            langs = detect_langs(text)
            best = langs[0]
            is_en = best.lang == 'en' and best.prob >= (0.70 if len(text) >= 20 else 0.40)

            if not is_en and len(text) < 20:
                is_en = is_english_short_text(text)
    
            lang_results.append(is_en)
        except LangDetectException:
            if is_english_short_text(text):
                lang_results.append(True)
            else:
                lang_results.append(None)

    lang_series = pd.Series(lang_results, index = df.index)

    report['because_langdetect_failed'] = int(lang_series.isna().sum())
    report['because_non_english'] = int((lang_series == False).sum())

    df = df[lang_series == True]
    
    report['kept_count'] = len(df)
    report['excluded_count'] = (report['because_empty'] + report['because_non_english'] + report['because_langdetect_failed'])

    return df.reset_index(drop = True), report


def collect_youtube_comments(api_key, video_ids, max_comments_per_video = None, max_replies_per_comment = None, collect_replies = False, language_filter = True, comment_order = 'relevance'):
    youtube = build('youtube', 'v3', developerKey = api_key)
    all_data = []
    all_reports = []

    print()
    pbar = tqdm(video_ids, desc = "Video processing status")

    for video_id in pbar:
        try:
            comments, page_reports = get_video_comments(youtube, video_id, max_results = max_comments_per_video, order = comment_order, language_filter = language_filter)

            all_data.extend(comments)
            all_reports.extend(page_reports)

            pbar.set_postfix({"Video ID": video_id, "Comments": len(comments)})

            if collect_replies and comments:
                replies = get_replies_to_comments(youtube, comments, max_replies = max_replies_per_comment)
                all_data.extend(replies)
        
        except HttpError as e:
            logging.error(f"Unexpected error while processing video {video_id}: {e}")
            continue

    data = pd.DataFrame(all_data)

    aggregated_report = {
        "initial_count": 0,
        "kept_count": 0,
        "excluded_count": 0,
        "because_empty": 0,
        "because_non_english": 0,
        "because_langdetect_failed": 0,
        "others": 0}        

    for report in all_reports:
        aggregated_report["initial_count"] += report.get("initial_count", 0)
        aggregated_report["because_empty"] += report.get("because_empty", 0)
        aggregated_report["because_non_english"] += report.get("because_non_english", 0)
        aggregated_report["because_langdetect_failed"] += report.get("because_langdetect_failed", 0)

    if language_filter:
        aggregated_report["kept_count"] = len(data[data["is_reply"] == False])
        aggregated_report["excluded_count"] = max(0, aggregated_report["initial_count"] - aggregated_report["kept_count"])
        explained_exclusions = (aggregated_report["because_empty"] + aggregated_report["because_non_english"] + aggregated_report["because_langdetect_failed"])
        aggregated_report["others"] = max(0, aggregated_report["excluded_count"] - explained_exclusions)

        filter_report = aggregated_report
    else:
        data, filter_report = clean_dataframe(data, language_filter = False)

    return data, filter_report
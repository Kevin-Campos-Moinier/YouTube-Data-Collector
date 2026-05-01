import re
# regex covering the vast majority of emojis.

EMOJI_PATTERN = re.compile(
    r'['
    r'\U0001F600-\U0001F64F'  # emoticons / smileys
    r'\U0001F300-\U0001F5FF'  # symbols & pictograms
    r'\U0001F680-\U0001F6FF'  # transport & maps
    r'\U0001F1E0-\U0001F1FF'  # flags
    r'\U00002702-\U000027B0'  # other symbols
    r'\U000024C2-\U0001F251'  # others
    r'\U0001F900-\U0001F9FF'  # other symbols and pictograms
    r'\U0001FA70-\U0001FAFF'  # other emojis (hearts, fire, hype, etc.) 
    r']+', flags = re.UNICODE)

 # Function to remove emojis from text
def remove_emojis(text):
    return EMOJI_PATTERN.sub('', text)
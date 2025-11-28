import requests
import feedparser
import logging
import os
import re
import json
from newspaper import Article
from datetime import datetime, date

# === IMPORT ESSENZIALI AWS/SERVERLESS ===
import boto3 
from botocore.exceptions import ClientError
from google import genai
from google.genai.errors import APIError

# Configura logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# === VARIABILI D'AMBIENTE ===
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID") 
DYNAMODB_TABLE_NAME = os.environ.get("DYNAMODB_TABLE_NAME") # NUOVA VARIABILE!

RSS_FEEDS = [
    'https://www.ansa.it/sito/ansait_rss.xml',
    'https://www.repubblica.it/rss/homepage/rss2.0.xml',
    'https://www.ilpost.it/feed/'
]

# === CONFIGURAZIONE GEMINI ===
client = None
MODEL_FLASH = 'gemini-2.5-flash'
MODEL_PRO = 'gemini-2.5-pro'

try:
    client = genai.Client() 
    logging.info(f"Client Gemini caricato.")
except Exception as e:
    logging.error(f"ERRORE: Impossibile caricare il client Gemini. Controlla GEMINI_API_KEY: {e}")
    client = None

# === CONFIGURAZIONE DYNAMODB ===
try:
    dynamodb = boto3.resource('dynamodb')
    table = dynamodb.Table(DYNAMODB_TABLE_NAME)
    logging.info(f"Connesso a DynamoDB: {DYNAMODB_TABLE_NAME}")
except Exception as e:
    logging.error(f"ERRORE: Impossibile connettersi a DynamoDB: {e}")
    table = None


# =========================================================
# === INTERFACCIA DYNAMODB (IMPLEMENTAZIONE REALE) ===
# =========================================================

def is_link_posted(link):
    """Controlla se un link è già stato postato utilizzando la tabella DynamoDB."""
    if not table: return False
    try:
        response = table.get_item(
            Key={'KeyType': 'LINK', 'KeyID': link}
        )
        return 'Item' in response
    except ClientError as e:
        logging.error(f"Errore DynamoDB (is_link_posted): {e.response['Error']['Message']}")
        return False
    except Exception as e:
        logging.error(f"Errore generico DB: {e}")
        return False

def mark_link_posted(link):
    """Segna un link come postato sulla tabella DynamoDB."""
    if not table: return
    try:
        table.put_item(
            Item={
                'KeyType': 'LINK',
                'KeyID': link,
                'PublishedAt': datetime.now().isoformat()
            }
        )
    except ClientError as e:
        logging.error(f"Errore DynamoDB (mark_link_posted): {e.response['Error']['Message']}")

def is_digest_sent_this_hour():
    """Controlla se il digest FLASH è già stato inviato per l'ora corrente."""
    if not table: return False
    current_hour_str = datetime.now().strftime("%Y-%m-%d-%H")
    try:
        response = table.get_item(
            Key={'KeyType': 'FLASH_DIGEST', 'KeyID': current_hour_str}
        )
        return 'Item' in response
    except ClientError as e:
        logging.error(f"Errore DynamoDB (is_digest_sent_this_hour): {e.response['Error']['Message']}")
        return False

def mark_digest_sent():
    """Segna che il digest FLASH è stato inviato per l'ora corrente."""
    if not table: return
    current_hour_str = datetime.now().strftime("%Y-%m-%d-%H")
    try:
        table.put_item(
            Item={
                'KeyType': 'FLASH_DIGEST',
                'KeyID': current_hour_str,
                'Timestamp': datetime.now().isoformat()
            }
        )
    except ClientError as e:
        logging.error(f"Errore DynamoDB (mark_digest_sent): {e.response['Error']['Message']}")
    
def is_pro_digest_sent_today():
    """Controlla se l'approfondimento PRO è già stato inviato per la data corrente."""
    if not table: return False
    today_str = date.today().isoformat()
    try:
        response = table.get_item(
            Key={'KeyType': 'PRO_DIGEST', 'KeyID': today_str}
        )
        return 'Item' in response
    except ClientError as e:
        logging.error(f"Errore DynamoDB (is_pro_digest_sent_today): {e.response['Error']['Message']}")
        return False

def mark_pro_digest_sent():
    """Segna che l'approfondimento PRO è stato inviato per la data corrente."""
    if not table: return
    today_str = date.today().isoformat()
    try:
        table.put_item(
            Item={
                'KeyType': 'PRO_DIGEST',
                'KeyID': today_str,
                'Timestamp': datetime.now().isoformat()
            }
        )
    except ClientError as e:
        logging.error(f"Errore DynamoDB (mark_pro_digest_sent): {e.response['Error']['Message']}")


# =========================================================
# === FINE INTERFACCIA DYNAMODB ===
# =========================================================

# === FUNZIONE PER ESTRARRE DATI ===
def estrai_dati(link):
    """Estrae l'articolo completo e il titolo dal link."""
    try:
        config = {
            'request_timeout': 10,
            'follow_meta_refresh': True,
            'browser_user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        articolo = Article(link, **config)
        articolo.download()
        articolo.parse()
        
        testo_completo = articolo.text 
        
        return articolo.top_image, articolo.title, testo_completo
    except Exception as e:
        logging.error(f"Errore nell'estrazione da {link}: {e}")
        return None, None, None

# === ESTRAI CONTENUTO BASE ===
def estrai_contenuto_base(entry):
    """Estrae contenuto base (titolo e summary) dal feed entry in caso di fallimento di Article."""
    titolo = entry.title if hasattr(entry, 'title') else "Notizia senza titolo"
    
    contenuto = ""
    if hasattr(entry, 'summary'):
        contenuto = re.sub('<[^<]+?>', '', entry.summary)
    elif hasattr(entry, 'description'):
        contenuto = re.sub('<[^<]+?>', '', entry.description)
    
    return titolo, contenuto[:500]


# === FUNZIONI DI FILTRAGGIO ===
def sono_simili(titolo1, titolo2):
    t1 = titolo1.lower().strip()
    t2 = titolo2.lower().strip()
    if t1 == t2: return True
    stop_words = {'il', 'lo', 'la', 'i', 'gli', 'le', 'un', 'una', 'di', 'da', 'in', 'per', 'con', 'su', 'a', 'è', 'e', 'che', 'del', 'della', 'dei'}
    parole1 = set([p for p in t1.split() if len(p) > 3 and p not in stop_words])
    parole2 = set([p for p in t2.split() if len(p) > 3 and p not in stop_words])
    
    if len(parole1) > 0 and len(parole2) > 0:
        comuni = len(parole1.intersection(parole2))
        percentuale = comuni / min(len(parole1), len(parole2))
        return percentuale > 0.6
    return False

def filtra_notizie_duplicate(notizie):
    notizie_filtrate = []
    for notizia in notizie:
        duplicata = False
        for n_filtrata in notizie_filtrate:
            if sono_simili(notizia['titolo'], n_filtrata['titolo']):
                duplicata = True
                break
        if not duplicata:
            notizie_filtrate.append(notizia)
    return notizie_filtrate


# === ANALISI CON GEMINI FLASH (Digest Orario) ===
def analizza_con_gemini_flash(notizie):
    """Usa Gemini FLASH per analizzare e organizzare le notizie orarie."""
    if not notizie or not client: return None
    
    try:
        testo_notizie = ""
        for i, notizia in enumerate(notizie, 1):
            testo_notizie += f"\n--- NOTIZIA {i} ---\n"
            testo_notizie += f"Titolo: {notizia['titolo']}\n"
            testo_notizie += f"Contenuto: {notizia['contenuto'][:1000]}\n" 
            testo_notizie += f"Link: {notizia['link']}\n"
        
        prompt = f"""Sei un giornalista professionista che cura un notiziario orario in italiano.
        
... [PROMPT OMESSO PER BREVITÀ, MA È QUELLO GIUSTO] ...

Rispondi SOLO con questo formato. Non includere introduzioni, saluti o testo aggiuntivo:

NOTIZIA 1
Titolo: [titolo originale]
Riassunto: [il tuo riassunto in 2-3 righe]
Link: [link originale]

... (fino a massimo 5 notizie)
"""
        response = client.models.generate_content(
            model=MODEL_FLASH,
            contents=prompt
        )
        
        return response.text if response and response.text else None
            
    except Exception as e:
        logging.error(f"Errore nell'analisi con Gemini FLASH: {e}")
        return None

# === ANALISI CON GEMINI PRO (Approfondimento Quotidiano) ===
def analizza_con_gemini_pro(notizie):
    """Usa Gemini PRO per l'analisi approfondita giornaliera."""
    if not notizie or not client: return None
    
    try:
        testo_notizie = ""
        for i, notizia in enumerate(notizie, 1):
            testo_notizie += f"### NOTIZIA {i}: {notizia['titolo']}\n"
            testo_notizie += f"{notizia['contenuto'][:8000]}\n\n" 

        prompt_pro = f"""Sei un analista di alto livello. Il tuo compito è fornire un "Approfondimento Quotidiano" in italiano.
        
... [PROMPT OMESSO PER BREVITÀ, MA È QUELLO GIUSTO] ...

TESTI DA ANALIZZARE:
---
{testo_notizie}
---
"""
        response = client.models.generate_content(
            model=MODEL_PRO,
            contents=prompt_pro
        )
        
        return response.text if response and response.text else None
            
    except Exception as e:
        logging.error(f"Errore nell'analisi con Gemini PRO: {e}")
        return None

# === PARSING RISPOSTA GEMINI ===
def parse_risposta_gemini(risposta_gemini):
    notizie_organizzate = []
    
    try:
        blocchi = re.split(r'NOTIZIA \d+', risposta_gemini)
        
        for blocco in blocchi[1:]:
            titolo_match = re.search(r'Titolo:\s*(.+?)(?:\n|$)', blocco)
            riassunto_match = re.search(r'Riassunto:\s*(.+?)(?=Link:|$)', blocco, re.DOTALL)
            link_match = re.search(r'Link:\s*(.+?)(?:\n|$)', blocco)
            
            if titolo_match and riassunto_match and link_match:
                notizie_organizzate.append({
                    'titolo': titolo_match.group(1).strip(),
                    'riassunto': riassunto_match.group(1).strip(),
                    'link': link_match.group(1).strip()
                })
        
        return notizie_organizzate[:5]
        
    except Exception as e:
        logging.error(f"Errore nel parsing della risposta Gemini: {e}")
        return []

# === RACCOLTA NOTIZIE COMUNE ===
def raccogli_notizie(max_per_feed=15, mark_posted=True):
    notizie = []
    
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:max_per_feed]: 
                link = entry.link
                
                if not is_link_posted(link):
                    immagine, titolo, testo_completo = estrai_dati(link)
                    
                    if not titolo and hasattr(entry, 'title'):
                        titolo = entry.title
                    
                    _, contenuto_base = estrai_contenuto_base(entry) 
                    contenuto_finale = testo_completo or contenuto_base

                    if titolo and contenuto_finale and len(contenuto_finale) > 100:
                        notizie.append({
                            'titolo': titolo,
                            'link': link,
                            'contenuto': contenuto_finale,
                            'immagine': immagine
                        })
                        if mark_posted:
                            mark_link_posted(link)
    
        except Exception as e:
            logging.error(f"Errore nel processare feed {url}: {e}")
            
    return filtra_notizie_duplicate(notizie)


# === CREA E INVIA DIGEST (FLASH) ===
def crea_e_invia_digest_flash(notizie_analizzate):
    if not notizie_analizzate:
        logging.info("Nessuna notizia da pubblicare")
        return False
    
    ora_attuale = datetime.now().strftime("%H:%M")
    messaggio = f"📰 <b>NOTIZIARIO LEONIA+ - Ore {ora_attuale}</b>\n"
    messaggio += f"<i>Analisi Rapida con Gemini Flash</i>\n\n"
    
    for i, notizia in enumerate(notizie_analizzate, 1):
        messaggio += f"<b>{i}. {notizia['titolo']}</b>\n"
        messaggio += f"{notizia['riassunto']}\n"
        messaggio += f"🔗 {notizia['link']}\n\n"
    
    try:
        response = requests.post(
            f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage',
            json={
                'chat_id': CHAT_ID,
                'text': messaggio,
                'parse_mode': 'HTML',
                'disable_web_page_preview': True
            }
        )
        if response.status_code == 200:
            logging.info(f"Digest FLASH pubblicato con {len(notizie_analizzate)} notizie")
            mark_digest_sent()
            return True
        else:
            logging.error(f"Errore Telegram FLASH: {response.text}")
            return False
    except Exception as e:
        logging.error(f"Errore nell'invio a Telegram (FLASH): {e}")
        return False

# === CREA E INVIA APPROFONDIMENTO (PRO) ===
def crea_e_invia_approfondimento_pro():
    logging.info("Preparazione dati per approfondimento PRO...")
    notizie_per_pro = raccogli_notizie(max_per_feed=5, mark_posted=False)[:15] 
    
    if not notizie_per_pro:
        logging.info("Nessuna notizia recente per l'approfondimento Pro.")
        mark_pro_digest_sent() 
        return False

    risposta_pro = analizza_con_gemini_pro(notizie_per_pro)
    
    if not risposta_pro:
        logging.error("Gemini PRO non ha risposto, salto l'approfondimento.")
        mark_pro_digest_sent()
        return False

    messaggio_pro = f"🧠 <b>APPROFONDIMENTO QUOTIDIANO</b>\n"
    messaggio_pro += f"<i>Analisi complessa con Gemini Pro</i>\n\n"
    messaggio_pro += risposta_pro

    try:
        response_tg = requests.post(
            f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage',
            json={
                'chat_id': CHAT_ID,
                'text': messaggio_pro,
                'parse_mode': 'HTML'
            }
        )
        
        if response_tg.status_code == 200:
            logging.info("Approfondimento Pro pubblicato con successo.")
            mark_pro_digest_sent()
            return True
        else:
            logging.error(f"Errore Telegram Approfondimento Pro: {response_tg.text}")
            return False
            
    except Exception as e:
        logging.error(f"Errore nell'invio a Telegram (PRO): {e}")
        return False

# === VERIFICA ORARIO ===
def is_orario_attivo():
    ora_corrente = datetime.now().hour
    return 6 <= ora_corrente < 21


# =========================================================
# === PUNTO DI INGRESSO (Entry Point) PER AWS LAMBDA ===
# =========================================================

def lambda_handler(event, context):
    """
    Funzione principale chiamata da Amazon EventBridge (Scheduler).
    """
    
    ora_attuale = datetime.now()
    logging.info(f"Esecuzione bot avviata all'ora: {ora_attuale.hour}:00")

    # Controlla se il DB è inizializzato (se l'inizializzazione è fallita all'inizio)
    if not table:
        logging.error("La connessione a DynamoDB non è riuscita. Impossibile garantire lo stato.")
        return {'statusCode': 500, 'body': json.dumps('DB Error.')}

    # 1. LOGICA APPROFONDIMENTO PRO (alle 18:00)
    if ora_attuale.hour == 18:
        if not is_pro_digest_sent_today():
            crea_e_invia_approfondimento_pro()
        else:
            logging.info("Approfondimento Pro già inviato oggi.")

    # 2. LOGICA DIGEST ORARIO FLASH (6:00 - 21:00)
    elif is_orario_attivo() and not is_digest_sent_this_hour():
        logging.info("Tentativo di digest orario (FLASH)...")
        
        notizie = raccogli_notizie(mark_posted=True)
        
        if notizie:
            risposta_gemini = analizza_con_gemini_flash(notizie)
            
            if risposta_gemini:
                notizie_organizzate = parse_risposta_gemini(risposta_gemini)
                if notizie_organizzate:
                    crea_e_invia_digest_flash(notizie_organizzate)
                else:
                    logging.error("Parsing risposta Gemini FLASH fallito")
                    mark_digest_sent() 
            else:
                logging.error("Gemini FLASH non ha risposto, salto questo digest")
                mark_digest_sent()
        else:
            logging.info("Nessuna notizia nuova trovata")
            mark_digest_sent()
    
    else:
        logging.info("Nessuna azione richiesta per l'ora corrente.")

    return {
        'statusCode': 200,
        'body': json.dumps('Bot logic executed successfully.')
    }

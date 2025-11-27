import feedparser
import requests
import time
import sqlite3
import logging
from newspaper import Article
from datetime import datetime, date
import nltk
import os
import re

# === NUOVI IMPORT E CONFIGURAZIONE GEMINI (Modern Python SDK) ===
from google import genai
from google.genai.errors import APIError

# Configura logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# === DOWNLOAD NLTK DATA ===
try:
    nltk.download('punkt', quiet=True)
    nltk.download('punkt_tab', quiet=True)
    nltk.download('averaged_perceptron_tagger', quiet=True)
except Exception as e:
    logging.warning(f"Alcuni download NLTK falliti: {e}")

# === VARIABILI D'AMBIENTE ===
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID", "@Lumenariaplusgiornale2")

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
    # Il client legge automaticamente GEMINI_API_KEY
    client = genai.Client() 
    logging.info(f"Client Gemini caricato. Modelli disponibili: {MODEL_FLASH} e {MODEL_PRO}")
except Exception as e:
    logging.error(f"ERRORE: Impossibile caricare il client Gemini. Variabile GEMINI_API_KEY mancante o errata: {e}")
    client = None


# === GESTIONE DATABASE ===

def init_db():
    conn = sqlite3.connect('news_bot.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS posted_links
                 (link TEXT PRIMARY KEY, published_at TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS posted_digests
                 (digest_time TIMESTAMP PRIMARY KEY)''')
    # NUOVA TABELLA: Traccia il digest Pro giornaliero
    c.execute('''CREATE TABLE IF NOT EXISTS posted_pro_digest
                 (digest_date DATE PRIMARY KEY)''')
    conn.commit()
    conn.close()

def is_link_posted(link):
    conn = sqlite3.connect('news_bot.db')
    c = conn.cursor()
    c.execute("SELECT 1 FROM posted_links WHERE link = ?", (link,))
    result = c.fetchone()
    conn.close()
    return result is not None

def mark_link_posted(link):
    conn = sqlite3.connect('news_bot.db')
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO posted_links (link, published_at) VALUES (?, ?)", 
              (link, datetime.now()))
    conn.commit()
    conn.close()

def is_digest_sent_this_hour():
    """Controlla se è già stato inviato un digest in quest'ora"""
    conn = sqlite3.connect('news_bot.db')
    c = conn.cursor()
    current_hour = datetime.now().replace(minute=0, second=0, microsecond=0)
    c.execute("SELECT 1 FROM posted_digests WHERE digest_time = ?", (current_hour,))
    result = c.fetchone()
    conn.close()
    return result is not None

def mark_digest_sent():
    """Segna che il digest di quest'ora è stato inviato"""
    conn = sqlite3.connect('news_bot.db')
    c = conn.cursor()
    current_hour = datetime.now().replace(minute=0, second=0, microsecond=0)
    c.execute("INSERT OR IGNORE INTO posted_digests (digest_time) VALUES (?)", (current_hour,))
    conn.commit()
    conn.close()
    
def is_pro_digest_sent_today():
    conn = sqlite3.connect('news_bot.db')
    c = conn.cursor()
    today = date.today().isoformat()
    c.execute("SELECT 1 FROM posted_pro_digest WHERE digest_date = ?", (today,))
    result = c.fetchone()
    conn.close()
    return result is not None

def mark_pro_digest_sent():
    conn = sqlite3.connect('news_bot.db')
    c = conn.cursor()
    today = date.today().isoformat()
    c.execute("INSERT OR IGNORE INTO posted_pro_digest (digest_date) VALUES (?)", (today,))
    conn.commit()
    conn.close()

# === FUNZIONE PER ESTRARRE DATI (invariata) ===

def estrai_dati(link):
    try:
        config = {
            'request_timeout': 10,
            'follow_meta_refresh': True,
            'browser_user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        articolo = Article(link, **config)
        articolo.download()
        articolo.parse()
        
        try:
            articolo.nlp()
            summary = articolo.summary
        except:
            summary = articolo.text[:500] + "..." if len(articolo.text) > 500 else articolo.text
            
        return articolo.top_image, articolo.title, summary, articolo.text
    except Exception as e:
        logging.error(f"Errore nell'estrazione da {link}: {e}")
        return None, None, None, None

# === ESTRAI CONTENUTO BASE (invariata) ===
def estrai_contenuto_base(entry):
    """Estrae contenuto base dal feed entry"""
    titolo = entry.title if hasattr(entry, 'title') else "Notizia senza titolo"
    
    contenuto = ""
    if hasattr(entry, 'summary'):
        contenuto = re.sub('<[^<]+?>', '', entry.summary)
    elif hasattr(entry, 'description'):
        contenuto = re.sub('<[^<]+?>', '', entry.description)
    
    return titolo, contenuto[:300]


# === FUNZIONI DI FILTRAGGIO (ripristinate dal tuo codice iniziale) ===

def sono_simili(titolo1, titolo2):
    """Controlla se due titoli sono troppo simili (stesso argomento)"""
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
    """Rimuove notizie duplicate o troppo simili"""
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
            # Limita l'input a Flash a 400 caratteri per velocità
            testo_notizie += f"Contenuto: {notizia['contenuto'][:400]}\n"
            testo_notizie += f"Link: {notizia['link']}\n"
        
        prompt = f"""Sei un giornalista professionista che cura un notiziario orario in italiano.

Analizza queste notizie e:
1. Elimina le notizie duplicate o molto simili (stesso argomento/evento).
2. Seleziona le 5 notizie più importanti e interessanti.
3. Organizzale per rilevanza (più importante prima).
4. Per ogni notizia selezionata, scrivi un riassunto conciso di 2-3 righe in italiano.

NOTIZIE DA ANALIZZARE:
{testo_notizie}

Rispondi SOLO con questo formato (niente altro testo):

NOTIZIA 1
Titolo: [titolo originale]
Riassunto: [il tuo riassunto in 2-3 righe]
Link: [link originale]

... (fino a massimo 5 notizie)
"""
        logging.info("Invio richiesta a Gemini FLASH...")
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
            # Invia più testo a Pro (max 8000 caratteri per articolo)
            testo_notizie += f"{notizia['contenuto'][:8000]}\n\n"

        prompt_pro = f"""Sei un analista di alto livello. Il tuo compito è fornire un "Approfondimento Quotidiano" in italiano.
        
        Analizza il seguente set di articoli e fai quanto segue:
        1.  **Identifica i 2-3 temi principali** del giorno tra le notizie fornite.
        2.  **Scrivi un riassunto analitico** e coeso di circa 4-5 paragrafi che colleghi i vari articoli, ne spieghi il contesto e ne valuti l'impatto potenziale.
        3.  **Suggerisci tre domande chiave** (Key Takeaways) che i lettori dovrebbero porsi per comprendere l'importanza degli eventi.
        
        Formato richiesto:
        * Titolo: 🧠 APPROFONDIMENTO: [Tema principale identificato]
        * Corpo: [L'analisi coesa in 4-5 paragrafi, usando tag HTML come <b>, <i>, <br>]
        * Takeaways: [Le tre domande chiave come lista]
        
        TESTI DA ANALIZZARE:
        ---
        {testo_notizie}
        ---
        """
        logging.info("Invio richiesta a Gemini PRO per approfondimento...")
        response = client.models.generate_content(
            model=MODEL_PRO,
            contents=prompt_pro
        )
        
        return response.text if response and response.text else None
            
    except Exception as e:
        logging.error(f"Errore nell'analisi con Gemini PRO: {e}")
        return None

# === PARSING RISPOSTA GEMINI (invariata) ===

def parse_risposta_gemini(risposta_gemini):
    """Estrae le notizie dalla risposta di Gemini (usato per il digest FLASH)"""
    notizie_organizzate = []
    
    try:
        # Questo parsing si aspetta l'output formattato dalla richiesta FLASH
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
    """Raccoglie notizie da tutti i feed."""
    notizie = []
    
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:max_per_feed]: 
                link = entry.link
                
                if not is_link_posted(link):
                    immagine, titolo, riassunto_nlp, testo_completo = estrai_dati(link)
                    
                    if not titolo and hasattr(entry, 'title'):
                        titolo = entry.title
                    
                    contenuto_finale = testo_completo or riassunto_nlp or estrai_contenuto_base(entry)[1]

                    if titolo and contenuto_finale:
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
    """Crea e invia il post orario con i risultati di FLASH."""
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
    """Raccoglie dati e invia l'approfondimento PRO."""
    logging.info("Preparazione dati per approfondimento PRO...")
    # Raccoglie i 5-10 articoli più recenti senza segnarli come "posted" di nuovo
    notizie_per_pro = raccogli_notizie(max_per_feed=3, mark_posted=False)[:10] 
    
    if not notizie_per_pro:
        logging.info("Nessuna notizia recente per l'approfondimento Pro.")
        mark_pro_digest_sent() # Segna per non riprovare subito
        return False

    risposta_pro = analizza_con_gemini_pro(notizie_per_pro)
    
    if not risposta_pro:
        logging.error("Gemini PRO non ha risposto, salto l'approfondimento.")
        mark_pro_digest_sent()
        return False

    # Assicurati di inviare il testo generato da PRO che DEVE contenere già i tag HTML
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
    """Verifica se siamo nell'orario di pubblicazione (6:00 - 21:00)"""
    ora_corrente = datetime.now().hour
    return 6 <= ora_corrente < 21

# === CICLO PRINCIPALE ===

def main_loop():
    while True:
        try:
            ora_attuale = datetime.now()
            
            # 1. LOGICA APPROFONDIMENTO PRO (alle 18:00)
            if ora_attuale.hour == 18 and not is_pro_digest_sent_today():
                logging.info("È l'ora dell'Approfondimento Pro (18:00).")
                crea_e_invia_approfondimento_pro()
                time.sleep(300) 
                continue

            # 2. LOGICA DIGEST ORARIO FLASH
            if not is_orario_attivo():
                logging.info("Fuori dall'orario di pubblicazione (6:00-21:00). In attesa...")
                time.sleep(300) 
                continue
            
            if is_digest_sent_this_hour():
                minuti_alla_prossima_ora = 60 - ora_attuale.minute
                logging.info(f"Digest già inviato per quest'ora. Prossimo tra {minuti_alla_prossima_ora} minuti...")
                time.sleep(300) 
                continue
            
            # Esegui il digest FLASH orario
            logging.info("Raccogliendo notizie per il digest orario (FLASH)...")
            notizie = raccogli_notizie(mark_posted=True)
            
            if notizie:
                logging.info(f"Raccolte {len(notizie)} notizie, invio a Gemini FLASH per analisi...")
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
            
            time.sleep(300) 
            
        except KeyboardInterrupt:
            logging.info("Bot fermato dall'utente")
            break
        except Exception as e:
            logging.error(f"Errore nel loop principale: {e}")
            time.sleep(300)

# === AVVIO ===
if __name__ == "__main__":
    init_db()
    logging.info("Bot notiziario avviato con Gemini AI...")
    logging.info("Pubblicherà digest orari (FLASH) dalle 6:00 alle 21:00 e Approfondimento (PRO) alle 18:00.")
    
    main_loop()

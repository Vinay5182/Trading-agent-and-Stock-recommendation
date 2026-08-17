import csv
import io
import urllib.request
from functools import lru_cache

from data_provider import (
    PROVIDER_TIMEOUT_SECONDS,
    build_tradingview_symbol,
    is_valid_market_symbol,
    normalize_symbol,
    provider_error_text,
    run_provider_call,
)


NSE_CSV = "NSE_CSV"
STATIC_FALLBACK = "STATIC_FALLBACK"

SUPPORTED_BASE_INDEXES = [
    "NIFTY_TOTAL_MARKET",
    "NIFTY_50",
    "NIFTY_NEXT_50",
    "NIFTY_100",
    "NIFTY_200",
    "NIFTY_500",
    "NIFTY_MIDCAP_50",
    "NIFTY_MIDCAP_100",
    "NIFTY_MIDCAP_150",
    "NIFTY_MIDCAP_SELECT",
    "NIFTY_SMALLCAP_50",
    "NIFTY_SMALLCAP_100",
    "NIFTY_SMALLCAP_250",
    "NIFTY_MICROCAP_250",
    "NIFTY_MIDSMALLCAP_400",
]

COMBINED_INDEXES = ["ALL_SUPPORTED", "BROAD_MARKET_750"]
NSE_CSV_BASE_URLS = [
    "https://nsearchives.nseindia.com/content/indices",
    "https://www.niftyindices.com/IndexConstituent",
]

CSV_SLUGS = {
    "NIFTY_TOTAL_MARKET": ["ind_niftytotalmarket_list.csv", "ind_niftytotalmarketlist.csv"],
    "NIFTY_50": ["ind_nifty50list.csv"],
    "NIFTY_NEXT_50": ["ind_niftynext50list.csv"],
    "NIFTY_100": ["ind_nifty100list.csv"],
    "NIFTY_200": ["ind_nifty200list.csv"],
    "NIFTY_500": ["ind_nifty500list.csv"],
    "NIFTY_MIDCAP_50": ["ind_niftymidcap50list.csv"],
    "NIFTY_MIDCAP_100": ["ind_niftymidcap100list.csv"],
    "NIFTY_MIDCAP_150": ["ind_niftymidcap150list.csv"],
    "NIFTY_MIDCAP_SELECT": ["ind_niftymidcapselectlist.csv"],
    "NIFTY_SMALLCAP_50": ["ind_niftysmallcap50list.csv"],
    "NIFTY_SMALLCAP_100": ["ind_niftysmallcap100list.csv"],
    "NIFTY_SMALLCAP_250": ["ind_niftysmallcap250list.csv"],
    "NIFTY_MICROCAP_250": ["ind_niftymicrocap250list.csv", "ind_niftymicrocap250_list.csv"],
    "NIFTY_MIDSMALLCAP_400": ["ind_niftymidsmallcap400list.csv"],
}

NIFTY_50_SYMBOLS = [
    "RELIANCE", "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK", "BAJAJ_AUTO",
    "BAJFINANCE", "BAJAJFINSV", "BEL", "BHARTIARTL", "BPCL", "BRITANNIA", "CIPLA",
    "COALINDIA", "DRREDDY", "EICHERMOT", "GRASIM", "HCLTECH", "HDFCBANK",
    "HDFCLIFE", "HEROMOTOCO", "HINDALCO", "HINDUNILVR", "ICICIBANK", "INDUSINDBK",
    "INFY", "ITC", "JIOFIN", "JSWSTEEL", "KOTAKBANK", "LT", "M&M", "MARUTI",
    "NESTLEIND", "NTPC", "ONGC", "POWERGRID", "SBILIFE", "SBIN",
    "SHRIRAMFIN", "SUNPHARMA", "TATACONSUM", "TATAMOTORS", "TATASTEEL", "TCS",
    "TECHM", "TITAN", "TRENT", "ULTRACEMCO",
]

NIFTY_NEXT_50_SYMBOLS = [
    "ABB", "ADANIENSOL", "ADANIGREEN", "ADANIPOWER", "AMBUJACEM", "BAJAJHLDNG",
    "BANKBARODA", "BOSCHLTD", "CANBK", "CHOLAFIN", "DABUR", "DIVISLAB", "DLF",
    "DMART", "GAIL", "GODREJCP", "HAL", "HAVELLS", "ICICIGI", "ICICIPRULI",
    "INDIGO", "IOC", "IRFC", "JINDALSTEL", "JSWENERGY", "LICI", "LODHA", "LTIM",
    "NAUKRI", "PFC", "PIDILITIND", "PNB", "RECLTD", "SHREECEM", "SIEMENS",
    "TATAPOWER", "TORNTPHARM", "TVSMOTOR", "UNITDSPR", "VBL", "VEDL", "WIPRO",
    "ZYDUSLIFE", "MOTHERSON", "HINDPETRO", "BHEL", "CUMMINSIND", "IRCTC", "POLYCAB",
    "INDHOTEL",
]

MID_SMALL_FALLBACK = [
    "ABCAPITAL", "ABFRL", "ALKEM", "ASHOKLEY", "ASTRAL", "AUBANK", "AUROPHARMA",
    "BALKRISIND", "BANDHANBNK", "BERGEPAINT", "BIOCON", "BSE", "CGPOWER", "COLPAL",
    "CONCOR", "CROMPTON", "DIXON", "FEDERALBNK", "FORTIS", "GLAND", "GMRINFRA",
    "GNFC", "HDFCAMC", "HINDZINC", "HUDCO", "IDEA", "IDFCFIRSTB", "IEX", "IGL",
    "INDIANB", "INDUSTOWER", "JUBLFOOD", "LALPATHLAB", "LAURUSLABS", "LICHSGFIN",
    "LUPIN", "MANKIND", "MARICO", "MAXHEALTH", "MFSL", "MPHASIS", "MRF", "NHPC",
    "NMDC", "OBEROIRLTY", "OFSS", "PAGEIND", "PATANJALI", "PETRONET", "PHOENIXLTD",
    "PIIND", "PRESTIGE", "SAIL", "SBICARD", "SRF", "SUPREMEIND", "SYNGENE",
    "TATACHEM", "TATAELXSI", "TIINDIA", "TORNTPOWER", "UNIONBANK", "UPL", "YESBANK",
    "ZOMATO", "360ONE", "AARTIIND", "AAVAS", "ACC", "ACE", "AFFLE", "AJANTPHARM",
    "APARINDS", "APLAPOLLO", "APLLTD", "ARE&M", "ATUL", "BATAINDIA", "BEML",
    "BLUEDART", "BLUESTARCO", "CAMS", "CARBORUNIV", "CASTROLIND", "CDSL",
    "CENTRALBK", "CENTURYPLY", "CESC", "CHALET", "COFORGE", "CREDITACC", "CYIENT",
    "DEEPAKNTR", "DEVYANI", "EIHOTEL", "EMAMILTD", "ENDURANCE", "ESCORTS",
    "EXIDEIND", "FACT", "FSL", "GLAXO", "GLENMARK", "GODREJPROP", "GRANULES",
    "GRAPHITE", "GUJGASLTD", "HAPPSTMNDS", "HFCL", "HONAUT", "IDBI", "IIFL",
    "IPCALAB", "JBCHEPHARM", "JKCEMENT", "JSL", "KAJARIACER", "KALYANKJIL",
    "KANSAINER", "KEI", "KFINTECH", "KPITTECH", "LINDEINDIA", "MAHABANK",
    "MANAPPURAM", "MAZDOCK", "MCX", "METROBRAND", "MGL", "MUTHOOTFIN",
    "NATIONALUM", "NAVINFLUOR", "NLCINDIA", "OIL", "PEL", "PERSISTENT", "POLICYBZR",
    "POONAWALLA", "RADICO", "RAMCOCEM", "RATNAMANI", "RBLBANK", "RELAXO", "ROUTE",
    "SCHAEFFLER", "SONACOMS", "SUNTV", "TANLA", "THERMAX", "TIMKEN", "TRIDENT",
    "TTML", "UCOBANK", "VGUARD", "VOLTAS", "WHIRLPOOL", "ZEEL",
]

EXTENDED_FALLBACK = [
    "AARTIDRUGS", "AARTIPHARM", "AETHER", "AKUMS", "ALIVUS", "ALKYLAMINE", "ANANDRATHI",
    "ANANTRAJ", "ANGELONE", "ANURAS", "ARVIND", "ASAHIINDIA", "ASHOKA", "ASTRAMICRO",
    "BALAMINES", "BALRAMCHIN", "BASF", "BAYERCROP", "BDL", "BIKAJI", "BIRLACORPN",
    "BLS", "BORORENEW", "BRIGADE", "CAPLIPOINT", "CEATLTD", "CELLO", "CERA",
    "CHAMBLFERT", "CHEMPLASTS", "CHENNPETRO", "CLEAN", "COCHINSHIP", "CONCORDBIO",
    "CRAFTSMAN", "CUB", "DATAPATTNS", "DCMSHRIRAM", "DELHIVERY", "DOMS", "ECLERX",
    "ELECON", "ELGIEQUIP", "EQUITASBNK", "ERIS", "FINCABLES", "FINEORG", "FINPIPE",
    "FIVESTAR", "GESHIP", "GILLETTE", "GMDCLTD", "GODFRYPHLP", "GPIL", "GPPL",
    "GRINDWELL", "GSPL", "HATSUN", "HBLPOWER", "HEG", "HOMEFIRST", "HSCL",
    "IIFLSEC", "INDIACEM", "INDIAMART", "INTELLECT", "IOB", "IRB", "J&KBANK",
    "JINDALSAW", "JKLAKSHMI", "JKPAPER", "JMFINANCIL", "JUBLINGREA", "JYOTHYLAB",
    "KARURVYSYA", "KEC", "KIRLOSBROS", "KIRLOSENG", "KPRMILL", "KRBL", "LATENTVIEW",
    "LEMONTREE", "LLOYDSME", "MAHLIFE", "MAPMYINDIA", "MEDANTA", "MEDPLUS",
    "MOTILALOFS", "MRPL", "NATCOPHARM", "NCC", "NEULANDLAB", "NH", "NIACL",
    "OLECTRA", "PNBHOUSING", "PRAJIND", "PRSMJOHNSN", "RAILTEL", "RAYMOND", "RITES",
    "RKFORGE", "RRKABEL", "SAPPHIRE", "SAREGAMA", "SOBHA", "SONATSOFTW", "SPARC",
    "STARHEALTH", "SUMICHEM", "SUNDARMFIN", "SUNDRMFAST", "SUVENPHAR", "SWANENERGY",
    "TATACOMM", "TATATECH", "TEAMLEASE", "TEXRAIL", "TRITURBINE", "UTIAMC", "VARROC",
    "WESTLIFE", "WELCORP", "WELSPUNLIV", "WELENT", "WOCKPHARMA", "ZENSARTECH", "ZENTEC",
]

TOTAL_MARKET_EXTRA_FALLBACK = [
    "20MICRONS", "21STCENMGM", "3IINFOLTD", "3MINDIA", "5PAISA", "63MOONS", "A2ZINFRA",
    "AAATECH", "AAKASH", "AAREYDRUGS", "AARON", "AARTECH", "AARTISURF", "AARVEEDEN",
    "AARVI", "ABAN", "ABBOTINDIA", "ABCOTS", "ABMINTLLTD", "ABSLAMC", "ACCELYA",
    "ACUTAAS", "ADFFOODS", "ADL", "ADORWELD", "AGARIND", "AGI", "AGIIL", "AGRITECH",
    "AHL", "AHLADA", "AHLEAST", "AHLUCONT", "AIAENG", "AIIL", "AIRAN", "AIROLAM",
    "AJMERA", "AKASH", "AKG", "AKI", "AKSHAR", "AKSHARCHEM", "AKSHOPTFBR", "ALANKIT",
    "ALBERTDAVD", "ALEMBICLTD", "ALICON", "ALKALI", "ALLCARGO", "ALLDIGI", "ALMONDZ",
    "AMBER", "AMBIKCO", "AMDIND", "AMIORG", "AMJLAND", "ANDHRAPAP", "ANDHRSUGAR",
    "ANIKINDS", "ANMOL", "ANSALAPI", "ANTGRAPHIC", "ANUP", "APEX", "APOLLO",
    "APOLLOPIPE", "APOLSINHOT", "APTECHT", "ARCHIDPLY", "ARCHIES", "ARIES", "ARIHANTCAP",
    "ARIHANTSUP", "ARMANFIN", "AROGRANITE", "ARROWGREEN", "ARSHIYA", "ARTEMISMED",
    "ARTNIRMAN", "ASAL", "ASHAPURMIN", "ASHIANA", "ASIANENE", "ASIANHOTNR", "ASIANTILES",
    "ASMS", "ASPINWALL", "ASTEC", "ASTRON", "ATL", "ATULAUTO", "AURIONPRO", "AUTOAXLES",
    "AUTOIND", "AVADHSUGAR", "AVANTIFEED", "AVG", "AVONMORE", "AVROIND", "AVTNPL",
    "AWFIS", "AWHCL", "AXISCADES", "AYMSYNTEX", "BAJAJCON", "BAJAJELEC", "BAJAJHIND",
    "BALAJITELE", "BALAXI", "BANARBEADS", "BANARISUG", "BARBEQUE", "BASML", "BBTC",
    "BCLIND", "BECTORFOOD", "BEDMUTHA", "BEPL", "BFUTILITIE", "BGRENERGY", "BHAGCHEM",
    "BHAGERIA", "BHAGYANGR", "BHANDARI", "BHARATFORG", "BHARATGEAR", "BHARATRAS",
    "BHARTIHEXA", "BIGBLOC", "BIL", "BINANIIND", "BINDALAGRO", "BIRLACABLE", "BIRLAMONEY",
    "BKMINDST", "BLAL", "BLISSGVS", "BODALCHEM", "BOMDYEING", "BOROLTD", "BOROSCI",
    "BPL", "BROOKS", "BSHSL", "BURNPUR", "BUTTERFLY", "BVCL", "CALSOFT", "CAMLINFINE",
    "CANTABIL", "CAPACITE", "CAPITALSFB", "CARERATING", "CCHHL", "CEIGALL", "CENTENKA",
    "CENTEXT", "CENTRUM", "CENTUM", "CEREBRAINT", "CHEMBOND", "CHEMCON", "CHEMFAB",
    "CHEVIOT", "CHOICEIN", "CLSEL", "COASTCORP", "COFFEEDAY", "COMPUSOFT", "CONSOFINVT",
    "CONTROLPR", "CORALFINAC", "CORDSCABLE", "COSMOFIRST", "COUNCODOS", "CREATIVE",
    "CREATIVEYE", "CREST", "CRISIL", "CROWN", "CSLFINANCE", "CYBERMEDIA", "CYBERTECH",
    "DALBHARAT", "DALMIASUG", "DAMODARIND", "DATAMATICS", "DAVANGERE", "DBOL", "DBREALTY",
    "DBSTOCKBRO", "DCAL", "DCM", "DCMFINSERV", "DCW", "DECCANCE", "DEEPINDS", "DEEPENR",
    "DELTACORP", "DEN", "DHAMPURSUG", "DHANBANK", "DHANI", "DHANUKA", "DHUNINV",
    "DIACABS", "DIAMINESQ", "DIGISPICE", "DISHTV", "DIVGIITTS", "DODLA", "DOLLAR",
    "DONEAR", "DPABHUSHAN", "DPSCLTD", "DREDGECORP", "DVL", "DWARKESH", "DYCL",
    "DYNAMATECH", "DYNPRO", "E2E", "EASEMYTRIP", "ECOBOARD", "EDELWEISS", "EIDPARRY",
    "EIHAHOTELS", "EKI", "ELDEHSG", "ELGIRUBCO", "EMAMIPAP", "EMBDL", "EMSLIMITED",
    "ENTERO", "EQUIPPP", "ESSARSHPNG", "ETHOSLTD", "EUROTEXIND", "EVEREADY", "EVERESTIND",
    "EXICOM", "EXPLEOSOL", "EXXARO", "FAIRCHEMOR", "FCL", "FDC", "FIBERWEB", "FIEMIND",
    "FILATEX", "FINCABLES", "FINEORG", "FINOPB", "FINPIPE", "FIVESTAR", "FLEXITUFF",
    "FOODSIN", "FORCEMOT", "GABRIEL", "GAEL", "GANDHAR", "GANECOS", "GANESHBE",
    "GANESHHOUC", "GATECH", "GATEWAY", "GEECEE", "GENERIC", "GENESYS", "GENUSPAPER",
    "GENUSPOWER", "GEOJITFSL", "GEPIL", "GICHSGFIN", "GILLANDERS", "GIPCL", "GKWLIMITED",
    "GLFL", "GLOBALVECT", "GLOBUSSPR", "GLOSTERLTD", "GODHA", "GOLDIAM", "GOODLUCK",
    "GOKEX", "GOKUL", "GOKULAGRO", "GRAUWEIL", "GRAVITA", "GREENLAM", "GREENPANEL",
    "GREENPLY", "GREENPOWER", "GRMOVER", "GROBTEA", "GSFC", "GTPL", "GULFOILLUB",
    "GULPOLY", "HATHWAY", "HCC", "HCG", "HGINFRA", "HIKAL", "HIL", "HILTON",
    "HIMATSEIDE", "HINDCOMPOS", "HINDCOPPER", "HINDMOTORS", "HINDOILEXP", "HINDWAREAP",
    "HISARMETAL", "HITECH", "HITECHCORP", "HLEGLAS", "HMAAGRO", "HMT", "HONASA",
    "HPAL", "HPL", "HTMEDIA", "HUBTOWN", "HUHTAMAKI", "ICRA", "ICIL", "IDEAFORGE",
    "IFCI", "IFBAGRO", "IFBIND", "IFGLEXPOR", "IGARASHI", "IKIO", "IMAGICAA",
    "INDIACEM", "INDIAMART", "INDNIPPON", "INDOCO", "INDORAMA", "INDOSTAR", "INDSWFTLAB",
    "INDSWFTLTD", "INOXGREEN", "INOXINDIA", "INOXWIND", "INSECTICID", "INSPIRISYS",
    "INTELLECT", "INTENTECH", "INVENTURE", "IRCON", "IRIS", "ISGEC", "ITDC",
    "ITDCEM", "JAGRAN", "JAGSNPHARM", "JAIBALAJI", "JAICORPLTD", "JASH", "JAYAGROGN",
    "JAYBARMARU", "JAYNECOIND", "JAYSREETEA", "JCHAC", "JINDALPOLY", "JINDRILL",
    "JISLJALEQS", "JITFINFRA", "JKIL", "JKTYRE", "JMA", "JPPOWER", "JSLHISAR",
    "JSWHL", "JTEKTINDIA", "JTLIND", "JUBLPHARMA", "JYOTICNC", "JYOTISTRUC", "KABRAEXTRU",
    "KAMATHOTEL", "KAMDHENU", "KANANIIND", "KAPSTON", "KARMAENG", "KAYA", "KCP",
    "KCPSUGIND", "KDDL", "KELLTONTEC", "KERNEX", "KESORAMIND", "KEYFINSERV", "KHADIM",
    "KHAICHEM", "KILITCH", "KIMS", "KINGFA", "KIOCL", "KIRIINDUS", "KIRLPNU",
    "KITEX", "KKCL", "KMSUGAR", "KOKUYOCMLN", "KOLTEPATIL", "KOPRAN", "KOTARISUG",
    "KOTHARIPET", "KOTHARIPRO", "KPIGREEN", "KRISHANA", "KRITI", "KRITINUT", "KSCL",
    "KSB", "KSOLVES", "KUANTUM", "LAGNAM", "LAOPALA", "LAXMIMACH", "LGBBROSLTD",
    "LGBFORGE", "LIBERTSHOE", "LIKHITHA", "LLOYDSENGG", "LLOYDSENT", "LOTUSEYE",
    "LOVABLE", "LTFOODS", "LUMAXIND", "LUMAXTECH", "LUXIND", "LXCHEM", "M&MFIN",
    "MAGADSUGAR", "MAGNUM", "MAHLOG", "MAHSCOOTER", "MAITHANALL", "MANAKALUCO",
    "MANAKCOAT", "MANAKSIA", "MANAKSTEEL", "MANGLMCEM", "MANGCHEFER", "MANINDS",
    "MANINFRA", "MARATHON", "MARINE", "MARKSANS", "MASFIN", "MASTEK", "MAXESTATES",
    "MAYURUNIQ", "MBAPL", "MBLINFRA", "MCL", "MCLEODRUSS", "MEDICAMEQ", "MENONBE",
    "MEGASOFT", "MHRIL", "MIDHANI", "MIRCELECTR", "MIRZAINT", "MMFL", "MMP",
    "MONARCH", "MOREPENLAB", "MOSCHIP", "MPSLTD", "MSTCLTD", "MTARTECH", "MUKANDLTD",
    "MUKTAARTS", "MUNJALAU", "MUNJALSHOW", "MURUDCERA", "MUTHOOTCAP", "NAHARCAP",
    "NAHARINDUS", "NAHARPOLY", "NAHARSPING", "NACLIND", "NDGL", "NDRAUTO", "NDTV",
    "NELCAST", "NELCO", "NEOGEN", "NETWEB", "NETWORK18", "NEWGEN", "NFL", "NGLFINE",
    "NIBE", "NILAINFRA", "NILASPACES", "NILKAMAL", "NITCO", "NITINSPIN", "NITIRAJ",
    "NIVABUPA", "NOCIL", "NSIL", "NUCLEUS", "NURECA", "ORBTEXP", "ORCHPHARMA",
    "ORICONENT", "ORIENTBELL", "ORIENTCEM", "ORIENTELEC", "ORIENTHOT", "ORIENTPPR",
    "ORISSAMINE", "OSWALAGRO", "PAISALO", "PANACEABIO", "PANAMAPET", "PARACABLES",
    "PARAGMILK", "PARAS", "PARSVNATH", "PASUPTAC", "PDMJEPAPER", "PEARLPOLY",
    "PENIND", "PENINLAND", "PGEL", "PGHL", "PGIL", "PCBL", "PFOCUS", "PIGL",
    "PILANIINVS", "PILITA", "PLASTIBLEN", "PLAZACABLE", "PNC", "POCL", "PODDARMENT",
    "POKARNA", "POLYMED", "POWERMECH", "PPAP", "PRICOLLTD", "PRIMESECU", "PRINCEPIPE",
    "PRITI", "PRITIKAUTO", "PROZONER", "PSB", "PSPPROJECT", "PTC", "PTL", "PUNJABCHEM",
    "PURVA", "QUESS", "QUICKHEAL", "RACE", "RADIANTCMS", "RAJRATAN", "RALLIS",
    "RAMASTEEL", "RAMCOSYS", "RAMKY", "RANEENGINE", "RANEHOLDIN", "RATEGAIN", "RBA",
    "RCF", "REDINGTON", "REFEX", "RELIGARE", "RGL", "RHIM", "RICOAUTO", "RIIL",
    "RISHABH", "RITCO", "RML", "ROLEXRINGS", "ROSSARI", "ROTO", "RPGLIFE", "RPOWER",
    "RSSOFTWARE", "RSYSTEMS", "RTNINDIA", "RTNPOWER", "RUBYMILLS", "RUCHIRA", "RUPA",
    "RUSHIL", "SAKSOFT", "SAKUMA", "SANDESH", "SANDHAR", "SANGAMIND", "SANGHIIND",
    "SANGHVIMOV", "SANOFI", "SANSERA", "SASTASUNDR", "SATIA", "SATIN", "SBCL",
    "SCHAND", "SDBL", "SEAMECLTD", "SECURKLOUD", "SENCO", "SEPC", "SEQUENT",
    "SERVOTECH", "SFL", "SGIL", "SHAKTIPUMP", "SHALBY", "SHALPAINTS", "SHANKARA",
    "SHARDACROP", "SHARDAMOTR", "SHAREINDIA", "SHEMAROO", "SHILPAMED", "SHIVALIK",
    "SHOPERSTOP", "SHRADHA", "SHREDIGCEM", "SHRIPISTON", "SHYAMCENT", "SHYAMMETL",
    "SIGACHI", "SIRCA", "SJS", "SMLISUZU", "SMSPHARMA", "SNOWMAN", "SOMANYCERA",
    "SOUTHWEST", "SPANDANA", "SPECIALITY", "SPENCERS", "SPLIL", "SPORTKING", "SSWL",
    "STARCEMENT", "STCINDIA", "STEELCAS", "STEELXIND", "STEL", "STERTOOLS", "STLTECH",
    "STOVEKRAFT", "STYLAMIND", "SUBROS", "SUDARSCHEM", "SUMMITSEC", "SUNCLAY",
    "SUNDARAM", "SUNFLAG", "SUNTECK", "SUPERSPIN", "SUPRAJIT", "SUPRIYA", "SURANASOL",
    "SURYAROSNI", "SUTLEJTEX", "SUZLON", "SVPGLOB", "SWARAJENG", "SWELECTES", "SYMPHONY",
    "TARC", "TARIL", "TASTYBITE", "TCI", "TCIEXP", "TCPLPACK", "TFCILTD", "THANGAMAYL",
    "THEINVEST", "THEMISMED", "THOMASCOOK", "TIDEWATER", "TIIL", "TIPSINDLTD",
    "TIRUMALCHM", "TMB", "TNPL", "TOKYOPLAST", "TRANSRAILL", "TREJHARA", "TRIVENI",
    "TVSSCS", "TVTODAY", "UDAICEMENT", "UGARSUGAR", "UJJIVANSFB", "UNICHEMLAB",
    "UNIECOM", "UNIENTER", "UNIPARTS", "UNIVCABLES", "UNOMINDA", "UTIAMC", "UTTAMSUGAR",
    "VADILALIND", "VAIBHAVGBL", "VAKRANGEE", "VALIANTORG", "VASCONEQ", "VENKEYS",
    "VESUVIUS", "VETO", "VHL", "VIDHIING", "VIJAYA", "VINATIORGA", "VINDHYATEL",
    "VIPIND", "VISAKAIND", "VISHNU", "VLSFINANCE", "VMART", "VOLTAMP", "VPRPL",
    "VRLLOG", "VSTIND", "VSTTILLERS", "WABAG", "WALCHANNAG", "WANBURY", "WENDT",
    "WHEELS", "WONDERLA", "WSTCSTPAPR", "XCHANGING", "XPROINDIA", "YASHO", "YATRA",
    "ZAGGLE", "ZEELEARN", "ZODIACLOTH", "ZOTA",
]


def clean_unique_symbols(symbols: list[str]) -> tuple[list[str], list[str]]:
    seen = set()
    result = []
    invalid = []
    for symbol in symbols:
        clean = normalize_symbol("NSE", symbol)
        if not is_valid_market_symbol(clean):
            if symbol and str(symbol).strip():
                invalid.append(str(symbol).strip().upper())
            continue
        if clean not in seen:
            seen.add(clean)
            result.append(clean)
    return result, invalid


def unique_symbols(symbols: list[str]) -> list[str]:
    result, _ = clean_unique_symbols(symbols)
    return result


def fallback_index_symbols() -> dict[str, list[str]]:
    nifty_50 = unique_symbols(NIFTY_50_SYMBOLS)
    nifty_next_50 = unique_symbols(NIFTY_NEXT_50_SYMBOLS)
    nifty_100 = unique_symbols(nifty_50 + nifty_next_50)
    total_pool = unique_symbols(
        nifty_100 + MID_SMALL_FALLBACK + EXTENDED_FALLBACK + TOTAL_MARKET_EXTRA_FALLBACK
    )
    nifty_200 = total_pool[:200]
    nifty_500 = total_pool[:500]
    midcap_150 = total_pool[100:250]
    smallcap_250 = total_pool[250:500]
    microcap_250 = total_pool[500:750]
    midsmallcap_400 = unique_symbols(midcap_150 + smallcap_250)[:400]
    total_market = unique_symbols(nifty_500 + microcap_250)[:750]
    return {
        "NIFTY_TOTAL_MARKET": total_market,
        "NIFTY_50": nifty_50,
        "NIFTY_NEXT_50": nifty_next_50,
        "NIFTY_100": nifty_100,
        "NIFTY_200": nifty_200,
        "NIFTY_500": nifty_500,
        "NIFTY_MIDCAP_50": midcap_150[:50],
        "NIFTY_MIDCAP_100": midcap_150[:100],
        "NIFTY_MIDCAP_150": midcap_150,
        "NIFTY_MIDCAP_SELECT": midcap_150[:25],
        "NIFTY_SMALLCAP_50": smallcap_250[:50],
        "NIFTY_SMALLCAP_100": smallcap_250[:100],
        "NIFTY_SMALLCAP_250": smallcap_250,
        "NIFTY_MICROCAP_250": microcap_250,
        "NIFTY_MIDSMALLCAP_400": midsmallcap_400,
    }


def fetch_nse_csv_symbols(index_name: str) -> tuple[list[str], str | None, int, list[str]]:
    errors = []
    for slug in CSV_SLUGS.get(index_name, []):
        for base_url in NSE_CSV_BASE_URLS:
            url = f"{base_url}/{slug}"
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "text/csv,*/*",
                    "Referer": "https://www.nseindia.com/",
                },
            )
            try:
                def read_csv():
                    with urllib.request.urlopen(request, timeout=PROVIDER_TIMEOUT_SECONDS) as response:
                        return response.read().decode("utf-8-sig", errors="ignore")

                text = run_provider_call("NSE", "CSV_UNIVERSE", read_csv)
                reader = csv.DictReader(io.StringIO(text))
                raw_symbols = [row.get("Symbol") or row.get("symbol") or "" for row in reader]
                symbols, invalid = clean_unique_symbols(raw_symbols)
                if symbols:
                    return symbols, None, len(raw_symbols), invalid
            except Exception as exc:
                errors.append(f"{url}: {provider_error_text('NSE', 'CSV_UNIVERSE', exc)}")
    return [], "; ".join(errors) if errors else "no_csv_slug", 0, []


@lru_cache(maxsize=64)
def resolve_base_index(index_name: str) -> tuple[tuple[str, ...], str, str | None, int, tuple[str, ...]]:
    clean_index = index_name.strip().upper()
    csv_symbols, error, raw_count, invalid = fetch_nse_csv_symbols(clean_index)
    if csv_symbols:
        return tuple(csv_symbols), NSE_CSV, None, raw_count, tuple(invalid)
    fallback = fallback_index_symbols().get(clean_index, [])
    return tuple(fallback), STATIC_FALLBACK, error, len(fallback), tuple()


def dedupe_universe(index_names: list[str]) -> tuple[list[dict], str, str | None]:
    by_symbol: dict[str, dict] = {}
    sources = set()
    errors = []
    raw_count = 0
    invalid_symbols = []
    for index_name in index_names:
        symbols, source, error, index_raw_count, invalid = resolve_base_index(index_name)
        raw_count += index_raw_count
        invalid_symbols.extend(invalid)
        sources.add(source)
        if error:
            errors.append(f"{index_name}: {error}")
        for symbol in symbols:
            row = by_symbol.setdefault(
                symbol,
                {
                    "exchange": "NSE",
                    "symbol": symbol,
                    "canonical_symbol": symbol,
                    "tradingview_symbol": build_tradingview_symbol("NSE", symbol),
                    "index_name": index_name,
                    "index_memberships": [],
                },
            )
            if index_name not in row["index_memberships"]:
                row["index_memberships"].append(index_name)
    source = NSE_CSV if sources == {NSE_CSV} else STATIC_FALLBACK if sources == {STATIC_FALLBACK} else "MIXED"
    rows = list(by_symbol.values())
    return rows, source, "; ".join(errors) if errors else None, raw_count, invalid_symbols


def get_universe_result(index_name: str) -> dict:
    clean_index = index_name.strip().upper()
    if clean_index in COMBINED_INDEXES:
        indexes = SUPPORTED_BASE_INDEXES if clean_index == "ALL_SUPPORTED" else ["NIFTY_TOTAL_MARKET"]
        rows, source, error, raw_count, invalid_symbols = dedupe_universe(indexes)
        return {
            "index_name": clean_index,
            "count": len(rows),
            "raw_count": raw_count,
            "cleaned_count": len(rows),
            "removed_invalid_count": len(invalid_symbols),
            "invalid_symbols_sample": invalid_symbols[:20],
            "symbols": rows,
            "source": source,
            "error": error,
        }

    symbols, source, error, raw_count, invalid_symbols = resolve_base_index(clean_index)
    rows = [
        {
            "exchange": "NSE",
            "symbol": symbol,
            "canonical_symbol": symbol,
            "tradingview_symbol": build_tradingview_symbol("NSE", symbol),
            "index_name": clean_index,
            "index_memberships": [clean_index],
        }
        for symbol in symbols
    ]
    return {
        "index_name": clean_index,
        "count": len(rows),
        "raw_count": raw_count,
        "cleaned_count": len(rows),
        "removed_invalid_count": len(invalid_symbols),
        "invalid_symbols_sample": list(invalid_symbols)[:20],
        "symbols": rows,
        "source": source,
        "error": error,
    }


def get_supported_indexes() -> dict:
    indexes = []
    for index_name in SUPPORTED_BASE_INDEXES + COMBINED_INDEXES:
        result = get_universe_result(index_name)
        indexes.append({
            "index_name": index_name,
            "count": result["count"],
            "source": result["source"],
            "error": result["error"],
        })
    all_supported = get_universe_result("ALL_SUPPORTED")
    broad = get_universe_result("BROAD_MARKET_750")
    return {
        "indexes": indexes,
        "all_supported_unique_count": all_supported["count"],
        "broad_market_750_unique_count": broad["count"],
    }


def get_universe(index_name: str) -> list[dict]:
    return get_universe_result(index_name)["symbols"]

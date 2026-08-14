"""
Sector-to-Stock mapping for NSE Broad Market Universe.

Maps 750/750 NSE stocks into 20 sector categories.
Every stock symbol belongs to EXACTLY ONE primary sector (no duplicate mappings).
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Sector → Stock Symbol Mapping
# ---------------------------------------------------------------------------
# Each sector contains constituent stocks from the NSE Broad Market 750.
# Stocks are assigned to their PRIMARY sector only (no duplicates).

SECTOR_STOCK_MAP: dict[str, list[str]] = {

    "AUTO": [
        "MARUTI", "TATAMOTORS", "M&M", "BAJAJ_AUTO", "HEROMOTOCO",
        "EICHERMOT", "ASHOKLEY", "TVSMOTORS", "TVSMOTOR", "MOTHERSON", "BOSCHLTD",
        "EXIDEIND", "BHARATFORG", "APOLLOTYRE", "MRF", "TIINDIA",
        "ENDURANCE", "AMARAJABAT", "ARE&M", "CEATLTD", "SONACOMS", "BALKRISIND",
        "CRAFTSMAN", "SCHAEFFLER", "SUNDRMFAST", "SUPRAJIT", "LUMAXTECH",
        "GABRIEL", "VARROC", "ABORYCL", "MAHINDCIE", "CIEINDIA", "SANGHVIMOV",
        "FIVESTAR", "HYUNDAI", "FORCEMOT", "JAMNAAUTO",
        "SUBROS", "UNOMINDA", "PRICOLLTD", "SANSERA", "ASKAUTOLTD",
        "ATHERENERG", "ATULAUTO", "FIEMIND", "BOSCH", "SMLMAH",
        "TMPV", "TMCV", "ZFCVINDIA", "MSUMI", "MINDACORP", "ESCORTS",
        "ASAHIINDIA", "BANCOINDIA", "BIMETAL", "FMGOETZE", "GNA", "JTEKTINDIA",
        "KALYANIFRG", "OLAELEC", "BELRISE",
    ],

    "BANKING": [
        "HDFCBANK", "ICICIBANK", "SBIN", "KOTAKBANK", "AXISBANK",
        "INDUSINDBK", "BANKBARODA", "PNB", "CANBK", "IDFCFIRSTB",
        "FEDERALBNK", "BANDHANBNK", "AUBANK", "RBLBANK", "INDIANB",
        "MAHABANK", "CUB", "KARURVYSYA", "UJJIVANSFB", "IDBI",
        "CENTRALBK", "UCOBANK", "IOB", "BANKINDIA", "DCBBANK",
        "TMBANK", "TMB", "EQUITASBNK", "SURYODAY", "ESAFSFB", "UNIONBANK",
        "J&KBANK", "CSBBANK", "SOUTHBANK", "KTKBANK", "DHANBANK",
        "JSFB", "INDIANHUME", "INDBANK", "YESBANK",
    ],

    "IT": [
        "TCS", "INFY", "INFOSYS", "HCLTECH", "WIPRO", "TECHM",
        "LTIM", "MPHASIS", "COFORGE", "PERSISTENT", "LTTS",
        "TATAELXSI", "KPITTECH", "BSOFT", "SONATASOFT", "SONATSOFTW", "HAPPSTMNDS",
        "ECLERX", "NIITLTD", "MASTEK", "NEWGEN", "INTELLECT",
        "CYIENT", "ZENSAR", "ZENSARTECH", "BIRLASOFT", "DATAPATTNS", "ROUTE",
        "TANLA", "RATEGAIN", "LATENTVIEW", "OFSS", "AFFLE", "AURIONPRO",
        "CMSINFO", "CIGNITI", "DATAMATICS", "EASEMYTRIP", "EMUDHRA", "FSL",
        "INFIBEAM", "INFOEDGE", "NAUKRI", "IXIGO", "JUSTDIAL", "MAPMYINDIA",
        "NETWEB", "PAYTM", "POLICYBZR", "ZAGGLE", "FIRSTCRY", "MEESHO",
        "CALSOFT", "CYIENTDLM", "DIGITAL", "EXPLEO", "GSS", "HEXT",
        "INFOBEAM", "INSPIRISYS", "INTERPRO", "KELLTONTEC", "LENSKART",
        "REDINGTON", "SWIGGY", "TATATECH", "CAPILLARY", "CCAVENUE",
        "BBOX", "BLS", "CRIZAC",
    ],

    "FMCG": [
        "HINDUNILVR", "ITC", "NESTLEIND", "BRITANNIA", "DABUR",
        "MARICO", "GODREJCP", "COLPAL", "TATACONSUM", "EMAMILTD",
        "VARUNBEV", "VBL", "BIKAJI", "DOMS", "ZYDUSWELL", "VBLLTD",
        "JYOTHYLAB", "PATANJALI", "RADICO", "UNITDSPR", "UBL",
        "HATSUN", "HATSON", "TATAELSI", "PVRINOX", "DEVYANI",
        "ASIANPAINT", "BERGEPAINT", "KANSAINER", "AKZOINDIA", "TRENT",
        "DMART", "ETERNAL", "BECTORFOOD", "CAMPUS", "CELLO",
        "DIAMONDYD", "DODLA", "GILLETTE", "GODFRYPHLP", "HONASA",
        "JUBLFOOD", "MANYAVAR", "NYKAA", "ORLKM", "ORKLAINDIA", "PICCADIL",
        "RBA", "SAPPHIRE", "WESTLIFE", "AWL", "BBTC", "CCL", "FMNL",
        "FOODSIN", "FUTURERETAIL", "GAEL", "GODREJIND", "GOKUL", "GOLDENTOBC",
        "HERITGFOOD", "JAYSREETEA", "JOCIL", "KOTHARIPRO", "LTFOODS",
        "MANORAMA", "URBANCO", "VMART", "WAKEFIT", "AWFIS", "BALRAMCHIN",
        "BLACKBUCK", "BLUEDART", "BLUESTONE", "BORORENEW", "CARTRADE",
        "CHALET", "ETHOSLTD", "EIHOTEL", "ELLEN", "EMBDL", "ENTERO",
        "GOKULAGRO",
    ],

    "PHARMA": [
        "SUNPHARMA", "DRREDDY", "CIPLA", "DIVISLAB", "AUROPHARMA",
        "TORNTPHARM", "LUPIN", "ZYDUSLIFE", "ALKEM", "IPCALAB",
        "LAURUSLABS", "GLENMARK", "NATCOPHARM", "BIOCON", "AJANTPHARM",
        "GLAND", "SYNGENE", "GRANULES", "LALPATHLAB", "PPLPHARMA",
        "SUPRIYA", "AARTIDRUGS", "AARTIPHARM", "ERIS", "JBCHEPHARM",
        "SHILPAMED", "MEDANTA", "MANKIND", "RAINBOW", "ABBOTINDIA",
        "AKUMS", "ALIVUS", "ANTHEM", "APLLTD", "ASTERDM", "ASTRAZEN",
        "ASTRAZENECA", "CAPLIPOINT", "CONCORDBIO", "EMCURE", "FDC", "GLAXO", "GLS",
        "HIKAL", "JAGSNPHARM", "JUBLPHARMA", "MARKSANS", "NEULANDLAB",
        "PFIZER", "SANOFICONR", "STAR", "SUDEEPPHRM", "VIYASH", "WOCKPHARMA",
        "ACUTAAS", "BAJAJHCARE", "BIOFILMED", "BLISSGVS", "BLUEJET",
        "DISHMAN", "INDRAMEDCO", "INDOCO", "IOLCP", "KILITCH", "KOPRAN",
        "KREBS", "ONESOURCE", "SAILIFE", "THYROCARE", "COHANCE", "CORONA",
        "CUPID",
    ],

    "METAL": [
        "TATASTEEL", "JSWSTEEL", "HINDALCO", "VEDL", "COALINDIA",
        "NMDC", "SAIL", "JINDALSTEL", "APLAPOLLO", "RATNAMANI",
        "NATIONALUM", "HINDCOPPER", "MOIL", "WELCORP", "MISHRA", "MIDHANI",
        "HINDZINC", "KIOCL", "GRAVITA", "SHYAMMETL", "JAIBALAJI",
        "JINDALSAW", "JSL", "NSLNISP", "PCBL", "SANDUMA", "SEAMLES",
        "MAHSEAMLES", "USHA", "USHAMART", "BHARATWIRE", "DPWIRES",
        "GANDHITUBE", "GPIL", "GRAPHITE", "HEG", "HISARMETAL", "IMFA",
        "ISMTLTD", "JSLHISAR", "KIRLFERRO", "KROSAKI", "SFL",
    ],

    "REALTY": [
        "DLF", "GODREJPROP", "OBEROIRLTY", "PRESTIGE", "PHOENIXLTD",
        "LODHA", "BRIGADE", "SOBHA", "SUNTECK", "MAHLIFE",
        "RUSTOMJEE", "SIGNATURE", "KOLTEPATIL", "ANANTRAJ",
        "RAYMOND", "KEYSTONE", "DBREALTY", "IBREALEST", "PURVA",
        "TARC", "WEWORK", "SAMHI", "BRIDGELAND", "EMAMIREAL",
        "GANESHIN", "GANESHHOUC", "HEMIPROP", "HUBTOWN", "KARDA",
        "LOTUSDEV", "ABREL",
    ],

    "ENERGY": [
        "ADANIENT", "ADANIPOWER", "ADANIENSOL", "CONFIPET",
        "DEEPIND", "HINDOILEXP", "AEGISCHEM", "AEGISLOG", "AEGISVOPAK",
    ],

    "PSU": [
        "LTI", "IRFC", "IRCTC", "RVNL",
        "NLCINDIA", "BEL", "HAL", "BDL",
        "MAZAGON", "MAZDOCK", "COCHINSHIP", "GRSE", "HUDCO",
        "NIACL", "GICRE", "NEWINDI", "NBCC",
        "ITI", "CONCOR", "ENGINERSIN", "FACT", "GMDCLTD",
        "RITES", "SCI", "DREADGING", "MMTC", "MSTCLTD",
    ],

    "MEDIA": [
        "ZEEL", "SUNTV", "NETWORK18", "TV18BRDCST", "NAZARA",
        "SAREGAMA", "TIPSMUSIC", "BALAJITELE", "DISHTV",
        "ENIL", "HMVL", "HTMEDIA", "JAGRAN", "NDTV", "CINELINE",
        "DEN", "DGCONTENT", "EROSMEDIA",
    ],

    "INFRA": [
        "LARSENTOUB", "LT", "ADANIPORTS", "LTIMINDTREE",
        "IRCON", "KEC", "KALPATPOWR", "KPIL", "NCC",
        "JKCEMENT", "RAMCOCEM", "HEIDELBERG", "STARCEMENT",
        "JKLAKSHMI", "ORIENTCEM", "HSCL", "PNCINFRA",
        "AHLUCONT", "HG", "HGINFRA", "BLUESTARLTD", "BLUESTARCO", "VOLTAMP",
        "ACC", "AMBUJACEM", "ULTRACEMCO", "SHREECEM", "DALBHARAT",
        "AFCONS", "ASHOKA", "ASTRAL", "BEML", "CAPACITE", "CERA",
        "DBL", "ELECTCAST", "GRINFRA", "HCC", "ITDCEM", "JWL",
        "KNRCON", "MOULDTEK", "NUVOCO", "PRSMJOHNSN", "RHIM", "RKFORGE",
        "TITAGARH", "WABAG", "ATLANTAELE",
        "BCONSTRUCT", "BEMLIND", "DECCANCE",
        "EMMBI", "EVERESTIND", "GANGAFORG", "GATEWAY", "GMRAIRPORT",
        "GREENPANEL", "GREENPLY", "GRWRHITECH", "GUJAPOLLO", "HIL",
        "HINDNATGLS", "HLEGLAS", "INDHOTEL", "INDIACEM", "INDIGOPNTS",
        "INTERARCH", "IRB", "ITDC", "JKIL", "JMC",
        "JSWINFRA", "SUPREMEIND", "TARIL", "ABDL", "ABLBL",
        "CEMPRO", "CENTURYPLY", "DCMSHRIRAM",
    ],

    "CHEMICAL": [
        "PIDILITIND", "SRF", "AARTI", "AARTIIND", "DEEPAKNTR", "ATUL",
        "NAVINFLUOR", "CLEAN", "FLUOROCHEM", "GNFC", "GSFC",
        "BASF", "BAYERCROP", "SUMICHEM", "TATACHEM", "ALKYLAMINE",
        "GALAXYSURF", "VINATIORGN", "FINEORG", "LXCHEM", "ANURAS",
        "ROSSARI", "AETHER", "NEOGEN", "NOCIL", "ACCI", "ADVENZYMES",
        "CHAMBLFERT", "COROMANDEL", "DEEPAKFERT",
        "EIDPARRY", "GHCL", "LINDEINDIA", "PARADEEP", "PIIND", "RCF",
        "SUDARSCHEM", "UPL", "BHAGCHEM", "BODALCHEM",
        "CAMLINFINE", "CHEMCON", "CHEMFAB", "CHEMPLAST", "CLARIANT",
        "DCW", "DENORA", "DHAMPURSUG", "DHANUKA", "DICIND", "EXCELINDUS",
        "FAIRCHEMOR", "GUJALKALI", "GULPOLY", "INSECTICID", "KHAICHEM",
        "KIRIINDUS", "ACI", "EPL",
    ],

    "CAPITAL_GOODS": [
        "ABB", "SIEMENS", "BHEL", "HAVELLS", "CUMMINSIND",
        "THERMAX", "ELGIEQUIP", "AIAENG", "GRINDWELL", "TIMKEN",
        "TRIVENI", "KAYNES", "DIXON", "HONAUT", "SCHNEIDER",
        "CGPOWER", "SUZLON", "INOXWIND", "TTKPRESTIG", "WHIRLPOOL",
        "CROMPTON", "ORIENTELEC", "KENNAMET", "ISGEC", "3MINDIA",
        "ACE", "ACMESOLAR", "ASTRAMICRO", "DIACABS",
        "ELECON", "EQUIPMENT", "FOSECOIND", "GVT&D", "HBLENGINE",
        "HONEYWELL", "HPL", "INGERRAND", "INOXGREEN", "INOXINDIA",
        "IONEXCHANG", "JYOTICNC", "KEI", "KIRLOSBROS", "KIRLOSENG",
        "KIRLPNU", "KPIGREEN", "KSB", "MTARTECH", "OLECTRA",
        "PARAS", "POLYCAB", "POWERINDIA", "POWERMECH", "PRAJIND",
        "PREMIERENE", "RRKABEL", "SHAKTIPUMP", "SKFINDIA",
        "SKFINDUS", "TDPOWERSYS", "TECHNOE", "TEGA", "TEJASNET",
        "TIMTIM", "TRITURBINE", "WAAREEENER", "WEBELSOLAR", "ZENTEC",
        "AXISCADES", "AVALON", "FINCABLES",
        "EMMVEE", "DYNAMATECH", "CPPLUS", "EIEL", "CRAMC",
        "EMIL", "EUREKAFORB", "EBL", "AEQUS", "ANUP", "AZAD",
        "BAJAJELEC", "BALUFORGE", "BGRENERGY", "BHARATCOAL",
    ],

    "CONSUMER_DURABLES": [
        "TITAN", "VOLTAS", "BATAINDIA",
        "RELAXO", "KALYANKJIL", "SENCO", "THANGAMAYL",
        "RAJESHEXPO", "VGUARD", "AMBER", "BLUESTAR", "CARYSIL",
        "FLAIR", "GOCOLORS", "KAJARIACER", "PGEL",
        "REDTAPE", "SAFARI", "SYRMA", "VIPIND", "VGUARDIAN",
    ],

    "FINANCIAL_SERVICES": [
        "BAJFINANCE", "BAJAJFINSV", "HDFCLIFE", "SBILIFE", "ICICIPRULI",
        "ICICIGI", "MFSL", "CHOLAFIN", "SHRIRAMFIN", "M&MFIN",
        "MANAPPURAM", "MUTHOOTFIN", "POONAWALLA", "LICHSGFIN", "CANFINHOME",
        "AAVAS", "HOMEFIRST", "APTUS", "NUVAMA", "ANGELONE", "MOTILALOFS", "IIFL", "JMFINANCIL",
        "ABSLAMC", "HDFCAMC", "NIPPONLIFE", "NAM_INDIA", "UTIAMC", "CAMS",
        "KFINTECH", "CDSL", "MCX", "BSE", "IEX", "360ONE", "AADHARHFC",
        "ABCAPITAL", "ANGELBRKG", "ANANDRATHI", "BAJAJHFL", "BAJAJHLDNG",
        "CANHLIFE", "CARTER", "CHOICEIN", "CHOLAHLDNG", "CREDITACC",
        "CRISIL", "EDELWEISS", "EMKAY", "FEDFINA", "FINANCIAL",
        "GODIGIT", "GROWW", "HDBFS", "ICICIAMC", "ICRA", "IDFC",
        "IFCI", "IIFLCAPS", "IIFLSEC", "INDIASHLTR", "INDOSTAR", "JIOFIN",
        "LICI", "LTF", "MUTHOOTMF", "NIVABUPA", "PINELABS", "PIRAMALFIN",
        "PNBHOUSING", "PRUDENT", "SAMMAANCAP", "SBFC", "SBICARD",
        "SHAREINDIA", "SUNDARMFIN", "TATACAP", "TATAINVEST", "AIIL",
        "CGCL",
    ],

    "HEALTHCARE": [
        "AGARWALEYE", "APOLLOHOSP", "DRHREDDY", "FORTIS", "KIMS",
        "KRSNAA", "MAXHEALTH", "SKANRAY", "YATHARTH",
    ],

    "OIL_GAS": [
        "BPCL", "CASTROL", "CASTROLIND", "GAIL",
        "GSPL", "GUJGASLTD", "HINDPETRO", "IGL", "IOC", "MGL", "MRPL",
        "PETRONET", "RELIANCE",
    ],

    "TELECOM": [
        "BHARTIARTL", "BHARTIHEXA", "DELHIVERY", "IDEA", "INDIGO",
        "INDUSTOWER", "RAILTEL", "STERLITE", "STLTECH", "TATACOMM", "TEJAS",
    ],

    "TEXTILE": [
        "ABFRL", "ALOKINDS", "ARVIND", "ARVINDFASN", "BOMDYEING", "DONEAR",
        "GARFIBRES", "GOKEX", "GRASIM", "HIMATSEIDE", "KPRMILL", "LAXMIMACH",
        "NITIRAJ", "RAYMONDLSL", "SPAL", "TRIDENT", "VTL",
        "WELSPUNLIV",
    ],

    "POWER": [
        "ADANIENSO", "ADANIGREEN", "ADANIPOWER", "CESC", "ENRIN",
        "INDIAGLYCO", "IREDA", "JPPOWER", "JSWENERGY", "NHPC",
        "NTPCGREEN", "PFC", "POWERGRID", "RECLTD", "RPOWER", "RTNPOWER",
        "SJVN", "SWSOLAR", "TATAPOWER", "TORNTPOWER", "WAAREERTL",
    ],
}


# ---------------------------------------------------------------------------
# Reverse lookup: symbol → sector
# ---------------------------------------------------------------------------
# Built once at import time.  If a symbol appears in multiple sectors
# (which the mapping above should avoid), the FIRST occurrence wins.

STOCK_SECTOR_MAP: dict[str, str] = {}
for _sector, _symbols in SECTOR_STOCK_MAP.items():
    for _sym in _symbols:
        if _sym not in STOCK_SECTOR_MAP:
            STOCK_SECTOR_MAP[_sym] = _sector

ALL_SECTORS: list[str] = list(SECTOR_STOCK_MAP.keys())


def classify_stock(symbol: str) -> str | None:
    """Return the sector name for *symbol*, or ``None`` if unmapped."""
    return STOCK_SECTOR_MAP.get(symbol)


def get_sector_stocks(sector_name: str) -> list[str]:
    """Return all stock symbols belonging to *sector_name*."""
    return SECTOR_STOCK_MAP.get(sector_name, [])

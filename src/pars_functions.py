import requests 
import logging
import time
import json
import random
import pandas as pd
from datetime import datetime
from pathlib import Path
from bs4 import BeautifulSoup
from urllib.parse import urlparse, parse_qs


# тут возможно подшаманить и для разных функций сделать разные лог-файлы
# потому что !возможно! буду делать параллельные процессы
handler = logging.FileHandler("parser_log.log", encoding="utf-8")
logging.basicConfig(level=logging.INFO, handlers=[handler],
                    format="%(asctime)s %(levelname)s %(message)s")

DATA_PATH = Path("data")
CACHE_PATH = Path("cache")
CACHE_PATH.mkdir(exist_ok=True)
EXC_REG = str(DATA_PATH / "regions_exclude.txt")
LINKS_PATH = str(DATA_PATH / "collected_links.txt")

BASE_URL = "https://auto.drom.ru"
CHOOSE_REG_URL = "https://www.drom.ru/my_region/"
REGION_URL = "https://auto.drom.ru/region{}/used/all/"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
                "AppleWebKit/537.36 (KHTML, like Gecko)"
                "Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1"
}

N_TOTAL = 16000

N_MAX = 200
N_MIN = 20

# настройку сессии перекинуть в другое место
session = requests.Session()
session.headers.update(HEADERS)

# regions_exclude.txt содержит регионы Казахстана и Беларуси, они не пригодятся (скорее всего)

def get_region_links(session: requests.Session,
                     choose_reg_url: str = CHOOSE_REG_URL, 
                     exc_reg_path: str = EXC_REG):
    """ 
        функция для получения ссылок на региональные страницы, выполняется один раз
    """
    if (DATA_PATH / "links_by_regions.csv").exists():
        logging.info("File links_by_regions.csv already exists, finishing get_region_links")
        return str(DATA_PATH / "links_by_regions.csv")
    
    with open(exc_reg_path, "r", encoding="utf-8") as f:
        exclude_regions = [reg.rstrip("\n") for reg in f.readlines()]

    links_by_region = {"region": [],
                       "link": []}

    logging.info(f"Sending request to {choose_reg_url}")
    try:
        response = session.get(choose_reg_url, timeout=20)
        response.raise_for_status()
    except Exception as e:
        logging.error(f"Request failed, status: {e}")
        return

    tree = response.text
    logging.info(f"Successfully got response, length: {len(tree)}")

    soup = BeautifulSoup(tree, "lxml")
    regions_body = soup.find_all("a", class_=["b-link", "regionLink"])
    if len(regions_body) == 0:
        return

    success = 0
    for body in regions_body:
        name = body.get_text()
        if not name:
            logging.warning(f"Cannot find text in `{body}`, continuing")
            continue
        logging.info(f"Find {name}")
        if name in exclude_regions:
            logging.info(f"{name} is in excluded regions, continuing")
            continue
        link = body["href"]
        if not link:
            logging.warning(f"Cannot find link in `{body}`, continuing")
            continue
        links_by_region["region"].append(name)
        links_by_region["link"].append(link)
        success += 1
        logging.info(f"Successfully added {name}: {link}, successfully processed {success} regions")

    df = pd.DataFrame(links_by_region)
    df.to_csv(str(DATA_PATH / "links_by_regions.csv"), index=False)
    logging.info("Created links_by_regions.csv")

    return str(DATA_PATH / "links_by_regions.csv")

def get_proportion(session: requests.Session, 
                   links_path: str, 
                   template: str = REGION_URL):
    """ функция для получения соотношения по регионам, будет выполняться не каждый раз, а например раз в неделю (но это не точно) """

    links_by_regions = pd.read_csv(links_path)
    regions_counts = {"region": [], "link": [], "count": []}

    success = 0
    for row in links_by_regions.itertuples():
        region, link = row.region, row.link
        qs = parse_qs(urlparse(link).query)
        region_code = qs.get("set_region", [None])[0]
        final_link = template.format(region_code)

        time.sleep(random.uniform(3.0, 6.0))
        logging.info(f"Sending request to {final_link}")
        try:
            response = session.get(final_link, timeout=20)
            response.raise_for_status()
        except Exception as e:
            logging.warning(f"Request failed, status: {e}, continuing")
            continue

        tree = response.text
        soup = BeautifulSoup(tree, "lxml")
        div = soup.find("div", attrs={"data-ga-stats-name": "bulls_counter"})
        if not div:
            continue

        payload_str = div.get("data-ga-stats-va-payload")
        if not payload_str:
            continue

        payload_dict = json.loads(payload_str)
        count = payload_dict.get("n_bulls")
        regions_counts["region"].append(region)
        regions_counts["link"].append(final_link)
        regions_counts["count"].append(count)
        success += 1
        logging.info(f"Successfully added {region}: {count} to regions_counts, processed {success} regions")
        print(f"{region},{final_link},{count}")

    df = pd.DataFrame(regions_counts)
    dt = datetime.now().strftime("%Y-%m-%d")
    df.to_csv(str(DATA_PATH / f"regions_counts_{dt}.csv"), index=False)
    logging.info(f"Created regions_counts_{dt}.csv")

    return str(DATA_PATH / f"regions_counts_{dt}.csv")

def get_listings(session: requests.Session,
                 region_link: str,
                 n_needed: int,
                 existing_links_path: Path = LINKS_PATH,
                 n_max: int = 200,
                 max_page: int = 10):
    # функция сбора новых ссылок
    if n_needed > n_max:
        logging.warning(f"n_needed ({n_needed}) > n_max ({n_max}), {n_max} records will be collected")
        n_needed = n_max

    with open(existing_links_path, "r", encoding="utf-8") as el:
        existing_links = el.readlines()

    new_links = []
    count = 0
    for page in range(1, max_page + 1):
        if count >= n_needed:
            logging.info(f"The required number of listings has been found")
            break
        page_link = region_link
        if page != 1:
            page_link += f"page{page}/"
        time.sleep(random.uniform(3.0, 6.0))
        try:
            logging.info(f"Sending request to {page_link}")
            response = session.get(page_link, timeout=20)
            response.raise_for_status()
        except Exception as e:
            logging.error(f"Request failed, status: {e}, continuing")
            continue

        tree = response.text
        soup = BeautifulSoup(tree, "lxml")
        listings_tree = soup.find("div", attrs={"data-bulletin-list": "true"})

        if not listings_tree:
            break

        listings = listings_tree.find_all("div", attrs={"data-ftid": "bulls-list_bull"})
        for i, listing in enumerate(listings, start=1):
            if count >= n_needed:
                logging.info(f"The required number of listings has been found")
                break
            link = listing.find("a")["href"]
            if link in existing_links:
                logging.info(f"Link {link} has already processed")
                continue
            if not link:
                logging.warning(f"Cannot find link of listing {i}, page link: {page_link}, continuing")
                continue
            logging.info(f"Found link of listing {i}: {link}")

            try:
                logging.info(f"Sending request to {link}")
                response_listing = session.get(link, timeout=20)
                response_listing.raise_for_status()
            except Exception as e:
                logging.error(f"Request failed, status: {e}, continuing")
                continue

            tree_listing = response_listing.text
            link_id = link.split("/")[-1]

            with open(str(CACHE_PATH / link_id), "w", encoding="utf-8") as ch:
                ch.write(tree_listing)
            
            new_links.append(f"{link}\n")
            count += 1

    logging.info(f"Found {len(new_links)} new listings")
    with open(existing_links_path, "a", encoding="utf-8") as el:
        el.writelines(new_links)

def pars_listings(region: str,
                  links_paths: list):
    cars = {}

    for lp in links_paths:
        with open(lp, "r", encoding="utf-8") as f:
            soup = BeautifulSoup(f.read(), "lxml")   
        
        properties = {}

        path = soup.find_all("span", class_="_1lj8ai62")

        if path:
            id = path[4].get_text().split()[-1]
            properties["brand"] = path[2].get_text()
            properties["model"] = path[3].get_text()

        properties["region"] = region

        properties["pub_date"] = soup.find("div", class_="css-v3k7ax ekfpm3y0").get_text().split()[-1]
        properties["price"] = soup.find("div", class_="wb9m8q1").get_text()

        properties_names = [tag.get_text() for tag in soup.find_all(attrs={"data-ftid": "property"})]
        properties_values = [tag.get_text() for tag in soup.find_all(attrs={"data-ftid": "value"})]

        for name, value in zip(properties_names, properties_values):
            properties[name] = value

        properties["is_sold"] = False | bool(soup.find(string="Автомобиль снят с продажи"))
        properties["n_images"] = len(soup.find("div", class_="css-10ib5jr").find_all("a"))

        seller_type = soup.find("div", class_="_1n15liu7")
        properties["seller_type"] = seller_type.get_text()

        properties["age"] = soup.find("div", class_="_1n15liu9").get_text()

        cars[id] = properties

    df = pd.DataFrame.from_dict(cars)
    df.to_csv("test_df.csv")
    
    return cars


        
def main():
    print(pars_listings("Владимирская область",
                        ["cache/279330945.html", "cache/297499385.html"]))


if __name__ == "__main__":
    main()

    


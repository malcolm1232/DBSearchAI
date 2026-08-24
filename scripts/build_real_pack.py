#!/usr/bin/env python3
"""Build the REAL golden pack (#473) from third-party public datasets.

The frozen `eval_fixtures/golden_pack` is model-authored: the same model family wrote
the documents, the questions AND the answers, then sat the exam. That circularity
compresses exactly the differences a retrieval bake-off exists to measure, so every
number measured on it is partly a self-grade.

This builder removes the model from the answer key entirely:

* **Content** is downloaded, not written - three unrelated public Kaggle datasets
  (Brazilian e-commerce, MovieLens, Baseball Databank).
* **Answers** are never authored. Every `key_facts` entry is produced by EXECUTING SQL
  against an independent sqlite engine (`dbsearch.eval.golden.stage2.gold_value`) over
  the frozen CSVs. A human authors the question and the query; the engine authors the
  answer.
* **Determinism** is total - fixed date windows and sorted stride sampling, never
  `random`. Re-running the builder reproduces the same `pack_hash`.

    python3 scripts/build_real_pack.py                   # build + validate
    python3 scripts/build_real_pack.py --out /tmp/pack   # elsewhere

The four stores deliberately split ONE business domain (Olist) across two stores, so a
cross-store join is a semantically real question rather than a contrived one - that is
the traversal capability (#35) the old pack could not measure.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dbsearch.eval.golden.stage2 import gold_value  # noqa: E402

DATASETS = {
    "olist": "olistbr/brazilian-ecommerce",
    "movielens": "shubhammehta21/movie-lens-small-latest-dataset",
    "baseball": "open-source-sports/baseball-databank",
}

# The frozen slice. Narrow windows keep the pack inlineable in one /router/compose body
# while preserving referential integrity: every child row's parent is present.
OLIST_MONTH = "2018-03"
OLIST_STRIDE = 3          # every 3rd order in the month, by sorted order_id
MOVIELENS_USERS = 50      # first N user ids, with all of their ratings and tags
BASEBALL_FROM = 2014      # seasons >= this year


# --------------------------------------------------------------------------------- #
# Source loading
# --------------------------------------------------------------------------------- #

def dataset_dirs() -> dict:
    """Resolve each dataset to a local directory, downloading on first use.

    kagglehub caches under ~/.cache/kagglehub, so this is a no-op once warm."""
    import kagglehub
    return {name: Path(kagglehub.dataset_download(ref)) for name, ref in DATASETS.items()}


def read_csv(path: Path) -> list:
    """Rows as dicts. utf-8-sig because Olist's category translation file ships a BOM,
    which would otherwise hide the first column behind a mangled key."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def project(rows: list, columns: list) -> list:
    return [[r[c] for c in columns] for r in rows]


# --------------------------------------------------------------------------------- #
# Table building - each returns (columns, rows) already trimmed and filtered
# --------------------------------------------------------------------------------- #

def build_olist(src: Path) -> dict:
    orders = [r for r in read_csv(src / "olist_orders_dataset.csv")
              if r["order_purchase_timestamp"].startswith(OLIST_MONTH)]
    orders.sort(key=lambda r: r["order_id"])
    orders = orders[::OLIST_STRIDE]
    order_ids = {r["order_id"] for r in orders}
    customer_ids = {r["customer_id"] for r in orders}

    items = [r for r in read_csv(src / "olist_order_items_dataset.csv")
             if r["order_id"] in order_ids]
    payments = [r for r in read_csv(src / "olist_order_payments_dataset.csv")
                if r["order_id"] in order_ids]
    reviews = [r for r in read_csv(src / "olist_order_reviews_dataset.csv")
               if r["order_id"] in order_ids]
    customers = [r for r in read_csv(src / "olist_customers_dataset.csv")
                 if r["customer_id"] in customer_ids]
    product_ids = {r["product_id"] for r in items}
    products = [r for r in read_csv(src / "olist_products_dataset.csv")
                if r["product_id"] in product_ids]
    categories = read_csv(src / "product_category_name_translation.csv")

    for rows, key in ((orders, "order_id"), (items, "order_id"), (payments, "order_id"),
                      (reviews, "review_id"), (customers, "customer_id"),
                      (products, "product_id")):
        rows.sort(key=lambda r: tuple(r.values()))
    categories.sort(key=lambda r: r["product_category_name"])

    return {
        "orders": (["order_id", "customer_id", "order_status", "order_purchase_timestamp",
                    "order_delivered_customer_date", "order_estimated_delivery_date"],
                   project(orders, ["order_id", "customer_id", "order_status",
                                    "order_purchase_timestamp",
                                    "order_delivered_customer_date",
                                    "order_estimated_delivery_date"])),
        "order_items": (["order_id", "order_item_id", "product_id", "seller_id", "price",
                         "freight_value"],
                        project(items, ["order_id", "order_item_id", "product_id",
                                        "seller_id", "price", "freight_value"])),
        "payments": (["order_id", "payment_sequential", "payment_type",
                      "payment_installments", "payment_value"],
                     project(payments, ["order_id", "payment_sequential", "payment_type",
                                        "payment_installments", "payment_value"])),
        "reviews": (["review_id", "order_id", "review_score", "review_creation_date"],
                    project(reviews, ["review_id", "order_id", "review_score",
                                      "review_creation_date"])),
        "customers": (["customer_id", "customer_unique_id", "customer_city",
                       "customer_state"],
                      project(customers, ["customer_id", "customer_unique_id",
                                          "customer_city", "customer_state"])),
        "products": (["product_id", "product_category_name"],
                     project(products, ["product_id", "product_category_name"])),
        "categories": (["product_category_name", "product_category_name_english"],
                       project(categories, ["product_category_name",
                                            "product_category_name_english"])),
    }


def build_movielens(src: Path) -> dict:
    ratings_all = read_csv(src / "ratings.csv")
    users = sorted({int(r["userId"]) for r in ratings_all})[:MOVIELENS_USERS]
    keep = set(users)
    ratings = [r for r in ratings_all if int(r["userId"]) in keep]
    tags = [r for r in read_csv(src / "tags.csv") if int(r["userId"]) in keep]
    movie_ids = {r["movieId"] for r in ratings} | {r["movieId"] for r in tags}
    movies = [r for r in read_csv(src / "movies.csv") if r["movieId"] in movie_ids]

    ratings.sort(key=lambda r: (int(r["userId"]), int(r["movieId"])))
    tags.sort(key=lambda r: (int(r["userId"]), int(r["movieId"]), r["tag"]))
    movies.sort(key=lambda r: int(r["movieId"]))
    return {
        "movies": (["movieId", "title", "genres"], project(movies, ["movieId", "title", "genres"])),
        "ratings": (["userId", "movieId", "rating"], project(ratings, ["userId", "movieId", "rating"])),
        "tags": (["userId", "movieId", "tag"], project(tags, ["userId", "movieId", "tag"])),
    }


def build_baseball(src: Path) -> dict:
    def recent(rows):
        return [r for r in rows if r["yearID"] and int(r["yearID"]) >= BASEBALL_FROM]

    batting = recent(read_csv(src / "Batting.csv"))
    salaries = recent(read_csv(src / "Salaries.csv"))
    teams = recent(read_csv(src / "Teams.csv"))
    player_ids = {r["playerID"] for r in batting} | {r["playerID"] for r in salaries}
    master = [r for r in read_csv(src / "Master.csv") if r["playerID"] in player_ids]

    bat_cols = ["playerID", "yearID", "teamID", "lgID", "G", "AB", "R", "H", "HR", "RBI", "BB", "SO"]
    team_cols = ["yearID", "teamID", "franchID", "name", "W", "L", "R", "HR", "attendance",
                 "DivWin", "WSWin"]
    player_cols = ["playerID", "nameFirst", "nameLast", "birthYear", "birthCountry", "bats", "throws"]

    # A blank in an otherwise-numeric column would make the gold engine sniff the whole
    # column as TEXT, so a gold SUM and the product's own SUM could land on different
    # types. Players missing any kept field are dropped rather than silently patched.
    master = [r for r in master if all(r[c] != "" for c in player_cols)]

    batting.sort(key=lambda r: (r["playerID"], int(r["yearID"]), r["teamID"], r["lgID"]))
    salaries.sort(key=lambda r: (int(r["yearID"]), r["teamID"], r["playerID"]))
    teams.sort(key=lambda r: (int(r["yearID"]), r["teamID"]))
    master.sort(key=lambda r: r["playerID"])
    return {
        "batting": (bat_cols, project(batting, bat_cols)),
        "salaries": (["yearID", "teamID", "lgID", "playerID", "salary"],
                     project(salaries, ["yearID", "teamID", "lgID", "playerID", "salary"])),
        "teams": (team_cols, project(teams, team_cols)),
        "players": (player_cols, project(master, player_cols)),
    }


STORE_TABLES = {
    "olist-orders": ("orders", "order_items", "payments", "reviews"),
    "olist-catalog": ("customers", "products", "categories"),
    "movielens": ("movies", "ratings", "tags"),
    "baseball": ("batting", "salaries", "teams", "players"),
}

# Authored descriptions of what each table and column MEANS (#486). `""` is the table's
# own description.
#
# The discipline that makes this a fair test, and keeps it legal: these describe MEANING,
# never VALUES. "how far the order got in its lifecycle" is metadata about the schema;
# "one of delivered, shipped, canceled" would be customer data in a prompt - the thing LAW
# 1 forbids and #479 exists to handle server-side. It would also hand the model the answer
# to E-005 and F-001 outright, which would make the measurement worthless.
SCHEMA_DESCRIPTIONS = {
    "olist-orders": {
        "orders": {
            "": "one row per customer order, with its lifecycle and delivery dates",
            "order_id": "unique id of the order; the key other order tables join on",
            "customer_id": "the buyer who placed it; joins to the customer master",
            "order_status": "how far the order got in its lifecycle - whether it completed,"
                            " was stopped before completing, or is still in progress",
            "order_purchase_timestamp": "when the buyer placed the order",
            "order_delivered_customer_date": "when the order actually reached the buyer;"
                                             " empty when it never did",
            "order_estimated_delivery_date": "the delivery date promised at purchase",
        },
        "order_items": {
            "": "one row per product line within an order; an order may have several",
            "order_id": "the order this line belongs to",
            "product_id": "the product sold on this line",
            "price": "the item price charged for this line, excluding freight",
            "freight_value": "the shipping cost charged for this line",
        },
        "payments": {
            "": "one row per payment record against an order; an order may be split",
            "payment_type": "the method the buyer paid with",
            "payment_installments": "how many instalments the payment was split into",
            "payment_value": "the amount of this payment",
        },
        "reviews": {
            "": "one row per buyer review of a completed order",
            "review_score": "the buyer's satisfaction rating for the order; higher is more"
                            " satisfied, lower is less",
            "review_creation_date": "when the review was collected",
        },
    },
    "olist-catalog": {
        "customers": {
            "": "the customer master - who the buyers are and where they are",
            "customer_id": "the buyer key that orders reference",
            "customer_city": "the buyer's city",
            "customer_state": "the buyer's state, as a two-letter code",
        },
        "products": {
            "": "the product master - every product the marketplace lists, one row each;"
                " its category is named in the marketplace's own language",
            # #489: this used to end "join to the translation table for the English name",
            # which reads as an instruction rather than a fact, and the model joined even
            # when the question never asked for an English name - dropping the one category
            # that has no translation row and answering 62 against a gold of 63. A
            # description states what a column IS; it must not prescribe a query shape.
            # #489 round 2: naming the categories table here at all pulls the model into
            # joining it, even for "how many distinct categories" - which drops the one
            # category with no translation row and answers 62 against a gold of 63. The
            # relationship is stated once, on the categories table, where it belongs.
            "product_category_name": "the product's own category name, in the"
                                     " marketplace's language",
        },
        "categories": {
            "": "translation table mapping each category key to its English name",
            "product_category_name": "the category key as used on products",
            "product_category_name_english": "the same category, named in English",
        },
    },
    "movielens": {
        "movies": {
            "": "the film catalogue; one row per film",
            "movieId": "the film key that ratings and tags reference",
            "title": "the film's title, with its release year in brackets",
            "genres": "all genres for the film in ONE field, separated by | - a film has"
                      " several, so match a genre as a substring rather than equality",
        },
        "ratings": {
            "": "one row per user per film - a viewer's star rating of a film",
            "rating": "the star rating the viewer gave; higher is better",
        },
        "tags": {"": "free-text keywords viewers attached to films"},
    },
    "baseball": {
        "batting": {
            "": "one row per player per season per club - a player's batting statistics",
            "yearID": "the season",
            "HR": "home runs hit - a batting statistic, nothing to do with pay or personnel",
            "RBI": "runs batted in",
            "AB": "at bats",
        },
        "salaries": {
            "": "what each player was paid, one row per player per season per club;"
                " a player's pay is identified by BOTH the season and the club",
            "yearID": "the season the pay applies to",
            "teamID": "the club paying it",
            "salary": "the player's pay for that season, in dollars",
        },
        "teams": {
            "": "one row per club per season - a club's season record;"
                " a club row is identified by BOTH the season and the club",
            "yearID": "the season",
            "teamID": "the club's short code, as used by salaries and batting",
            "name": "the club's full name",
            "W": "games won that season",
            "L": "games lost that season",
            "HR": "home runs the club hit that season",
            "attendance": "total home attendance that season",
        },
        "players": {
            "": "the player register - biographical facts about each player",
            "birthCountry": "the country the player was born in, often abbreviated",
            "bats": "which side the player bats from",
        },
    },
}

STORE_META = {
    "olist-orders": {
        "kind": "sql", "title": "Olist Orders", "business_unit": "commerce",
        "description": ("Brazilian online marketplace order transactions: order headers with "
                        "status and delivery dates, the line items and their prices and freight, "
                        "the payment records by payment type and instalments, and buyer review "
                        "scores per order."),
    },
    "olist-catalog": {
        "kind": "sql", "title": "Olist Catalog", "business_unit": "commerce",
        "description": ("Brazilian marketplace reference data: the customer master with city and "
                        "state, the product master with its category key, and the category name "
                        "translation table mapping Portuguese category keys to English names."),
    },
    "movielens": {
        "kind": "sql", "title": "MovieLens", "business_unit": "media",
        "description": ("Film catalogue and viewer feedback: movie titles with release year and "
                        "pipe-separated genres, per-user star ratings of those movies, and free-text "
                        "tags users attached to films."),
    },
    "baseball": {
        "kind": "sql", "title": "Baseball Databank", "business_unit": "sports",
        "description": ("Major League Baseball season statistics: per-player per-season batting "
                        "lines, player salaries by season and club, team season records with wins, "
                        "losses and attendance, and the player biographical register."),
    },
}


# --------------------------------------------------------------------------------- #
# Questions. `sql` yields a NUMERIC scalar and becomes gold_sql (execution-accuracy
# scored, and re-checked by the pack validator). `derive` yields a STRING scalar: the
# scorer's execution-accuracy path is numeric-only, so those questions carry no gold_sql
# and are scored on fact recall - but the fact is still ENGINE-derived, never authored.
# --------------------------------------------------------------------------------- #

QUESTIONS = [
    # --- A: single store, single table -------------------------------------------- #
    dict(id="A-001", capability="A", stores=["olist-orders"], table="orders",
         q="How many orders reached delivered status?",
         sql="SELECT COUNT(*) FROM orders WHERE order_status='delivered'"),
    dict(id="A-002", capability="A", stores=["olist-orders"], table="payments",
         q="What is the total value of all credit card payments?",
         sql="SELECT SUM(payment_value) FROM payments WHERE payment_type='credit_card'"),
    dict(id="A-003", capability="A", stores=["movielens"], table="ratings",
         q="How many individual star ratings are recorded in total?",
         sql="SELECT COUNT(*) FROM ratings"),
    dict(id="A-004", capability="A", stores=["baseball"], table="salaries",
         q="What is the highest single season salary on record?",
         sql="SELECT MAX(salary) FROM salaries"),
    dict(id="A-005", capability="A", stores=["baseball"], table="players",
         q="How many players in the register bat left-handed?",
         sql="SELECT COUNT(*) FROM players WHERE bats='L'"),
    # Deliberately asked as a count, not "which state?". The stored value is the code
    # 'SP', and a correct answer that writes "Sao Paulo" would be scored wrong by a
    # word-anchored phrase match - that is the #463 artifact class, and a brand-new pack
    # should not import it.
    dict(id="A-006", capability="A", stores=["olist-catalog"], table="customers",
         q="How many customers are based in the state of SP?",
         sql="SELECT COUNT(*) FROM customers WHERE customer_state='SP'"),

    # --- B: single store, aggregate over a group ---------------------------------- #
    dict(id="B-001", capability="B", stores=["olist-orders"], table="reviews",
         q="What is the average review score buyers left on their orders?",
         sql="SELECT AVG(review_score) FROM reviews"),
    dict(id="B-002", capability="B", stores=["movielens"], table="ratings",
         q="What is the average star rating viewers gave to films?",
         sql="SELECT AVG(rating) FROM ratings"),
    dict(id="B-003", capability="B", stores=["baseball"], table="teams",
         q="What is the highest number of home runs any club hit in the 2015 season?",
         sql="SELECT MAX(HR) FROM teams WHERE yearID=2015"),
    dict(id="B-004", capability="B", stores=["olist-catalog"], table="products",
         q="How many distinct product categories does the catalogue contain?",
         sql="SELECT COUNT(DISTINCT product_category_name) FROM products"),
    dict(id="B-005", capability="B", stores=["olist-orders"], table="order_items",
         q="What is the total freight charged across every order line?",
         sql="SELECT SUM(freight_value) FROM order_items"),
    dict(id="B-006", capability="B", stores=["baseball"], table="batting",
         q="How many home runs were hit in the 2015 season in total?",
         sql="SELECT SUM(HR) FROM batting WHERE yearID=2015"),

    # --- C: join WITHIN one store ------------------------------------------------- #
    dict(id="C-001", capability="C", stores=["olist-orders"], table="order_items",
         q="What is the total item price across orders that were paid by boleto?",
         sql=("SELECT SUM(i.price) FROM order_items i "
              "JOIN payments p ON p.order_id=i.order_id WHERE p.payment_type='boleto'")),
    dict(id="C-002", capability="C", stores=["movielens"], table="ratings",
         q="What is the average rating of the film Toy Story (1995)?",
         sql=("SELECT AVG(r.rating) FROM ratings r JOIN movies m ON m.movieId=r.movieId "
              "WHERE m.title='Toy Story (1995)'")),
    dict(id="C-003", capability="C", stores=["baseball"], table="salaries",
         q="What was the total payroll of the Boston Red Sox in the 2015 season?",
         sql=("SELECT SUM(s.salary) FROM salaries s JOIN teams t "
              "ON t.teamID=s.teamID AND t.yearID=s.yearID "
              "WHERE t.name='Boston Red Sox' AND s.yearID=2015")),
    # The year suffix is stripped from the derived title: "Forrest Gump (1994)" as a
    # key_fact would fail a correct answer that just names the film, while the bare
    # title still matches an answer that includes the year.
    dict(id="C-004", capability="C", stores=["movielens"], table="movies",
         q="Which film received the greatest number of ratings?",
         derive=("SELECT RTRIM(SUBSTR(m.title, 1, INSTR(m.title,' (')-1)) FROM ratings r "
                 "JOIN movies m ON m.movieId=r.movieId WHERE INSTR(m.title,' (')>0 "
                 "GROUP BY m.title ORDER BY COUNT(*) DESC, m.title LIMIT 1")),
    dict(id="C-005", capability="C", stores=["olist-orders"], table="payments",
         q="What is the average payment value on orders that were cancelled?",
         sql=("SELECT AVG(p.payment_value) FROM payments p "
              "JOIN orders o ON o.order_id=p.order_id WHERE o.order_status='canceled'")),

    # --- D: join ACROSS two stores (the traversal capability) --------------------- #
    dict(id="D-001", capability="D", stores=["olist-orders", "olist-catalog"], table="order_items",
         q="What is the total item revenue from customers located in the state of RJ?",
         sql=("SELECT SUM(i.price) FROM order_items i "
              "JOIN orders o ON o.order_id=i.order_id "
              "JOIN customers c ON c.customer_id=o.customer_id WHERE c.customer_state='RJ'")),
    dict(id="D-002", capability="D", stores=["olist-orders", "olist-catalog"], table="order_items",
         q="How many order lines were for products in the health and beauty category?",
         sql=("SELECT COUNT(*) FROM order_items i "
              "JOIN products p ON p.product_id=i.product_id "
              "JOIN categories c ON c.product_category_name=p.product_category_name "
              "WHERE c.product_category_name_english='health_beauty'")),
    dict(id="D-003", capability="D", stores=["olist-orders", "olist-catalog"], table="reviews",
         q="What is the average review score given by customers in the state of SP?",
         sql=("SELECT AVG(r.review_score) FROM reviews r "
              "JOIN orders o ON o.order_id=r.order_id "
              "JOIN customers c ON c.customer_id=o.customer_id WHERE c.customer_state='SP'")),
    # Asked for the figure rather than the category name: the stored English names are
    # snake_case ('health_beauty'), so naming the winner would score a correct
    # "health and beauty" as a miss.
    dict(id="D-004", capability="D", stores=["olist-orders", "olist-catalog"], table="order_items",
         q="What total item revenue did the single best-selling product category generate?",
         sql=("SELECT SUM(i.price) FROM order_items i "
              "JOIN products p ON p.product_id=i.product_id "
              "JOIN categories c ON c.product_category_name=p.product_category_name "
              "GROUP BY c.product_category_name_english "
              "ORDER BY SUM(i.price) DESC LIMIT 1")),
    dict(id="D-005", capability="D", stores=["olist-orders", "olist-catalog"], table="orders",
         q="How many delivered orders were placed by customers in the city of sao paulo?",
         sql=("SELECT COUNT(*) FROM orders o JOIN customers c ON c.customer_id=o.customer_id "
              "WHERE c.customer_city='sao paulo' AND o.order_status='delivered'")),

    # --- E: value linking - the user's wording is not the stored literal (#462) ---- #
    dict(id="E-001", capability="E", stores=["olist-orders"], table="payments",
         q="How much did customers pay using debit cards?",
         sql="SELECT SUM(payment_value) FROM payments WHERE payment_type='debit_card'",
         hardness=["value-encoding"]),
    dict(id="E-002", capability="E", stores=["movielens"], table="movies",
         q="How many films in the catalogue are science fiction?",
         sql="SELECT COUNT(*) FROM movies WHERE genres LIKE '%Sci-Fi%'",
         hardness=["value-encoding"]),
    dict(id="E-003", capability="E", stores=["baseball"], table="players",
         q="How many players in the register were born in the Dominican Republic?",
         sql="SELECT COUNT(*) FROM players WHERE birthCountry='D.R.'",
         hardness=["value-encoding"]),
    dict(id="E-004", capability="E", stores=["olist-orders", "olist-catalog"], table="order_items",
         q="What was the total item revenue from bed, bath and table products?",
         sql=("SELECT SUM(i.price) FROM order_items i "
              "JOIN products p ON p.product_id=i.product_id "
              "JOIN categories c ON c.product_category_name=p.product_category_name "
              "WHERE c.product_category_name_english='bed_bath_table'"),
         hardness=["value-encoding"]),
    dict(id="E-005", capability="E", stores=["olist-orders"], table="orders",
         q="How many orders were called off before delivery?",
         sql="SELECT COUNT(*) FROM orders WHERE order_status='canceled'",
         hardness=["value-encoding"]),

    # --- F: paraphrase / wrong-vocab variants ------------------------------------- #
    dict(id="F-001", capability="F", stores=["olist-orders"], table="orders",
         q="How many parcels actually made it into the buyer's hands?",
         sql="SELECT COUNT(*) FROM orders WHERE order_status='delivered'",
         variant_of="A-001", hardness=["wrong-vocab"]),
    dict(id="F-002", capability="F", stores=["movielens"], table="ratings",
         q="On average, how highly did the audience score the first Toy Story picture?",
         sql=("SELECT AVG(r.rating) FROM ratings r JOIN movies m ON m.movieId=r.movieId "
              "WHERE m.title='Toy Story (1995)'"),
         variant_of="C-002", hardness=["wrong-vocab"]),
    # Reworded 260803: "once their purchase arrived" implies a delivery filter, and once
    # #490 made IS NULL work the model correctly applied one and scored 3.845 against a
    # gold of 3.79 computed over ALL reviews. The QUESTION was wrong, not the answer - a
    # wrong-vocab variant must restate B-001, not quietly narrow it.
    dict(id="F-003", capability="F", stores=["olist-orders"], table="reviews",
         q="Typically, how happy were shoppers with what they bought?",
         sql="SELECT AVG(review_score) FROM reviews",
         variant_of="B-001", hardness=["wrong-vocab"]),
    dict(id="F-004", capability="F", stores=["baseball"], table="salaries",
         q="What is the fattest single-year pay packet a ballplayer took home?",
         sql="SELECT MAX(salary) FROM salaries",
         variant_of="A-004", hardness=["wrong-vocab"]),
    dict(id="F-005", capability="F", stores=["olist-orders", "olist-catalog"], table="order_items",
         q="How much money did shoppers from Rio de Janeiro's state put through the marketplace, "
           "counting item prices only?",
         sql=("SELECT SUM(i.price) FROM order_items i "
              "JOIN orders o ON o.order_id=i.order_id "
              "JOIN customers c ON c.customer_id=o.customer_id WHERE c.customer_state='RJ'"),
         variant_of="D-001", hardness=["wrong-vocab"]),

    # --- G: unanswerable - the honest answer is a refusal (#467) ------------------- #
    # G-001 is the sharpest: "salary" and "compensation" DO exist here, in a baseball
    # payroll table. A model that answers from it has fabricated an HR system.
    dict(id="G-001", capability="G",
         q="What is the average employee salary in the HR compensation database?",
         answerable=False),
    dict(id="G-002", capability="G",
         q="How many support tickets were escalated to tier three last quarter?",
         answerable=False),
    dict(id="G-003", capability="G",
         q="What is the monthly churn rate in the subscriptions database?",
         answerable=False),
    dict(id="G-004", capability="G",
         q="Which supplier has the longest lead time in the procurement system?",
         answerable=False),
    dict(id="G-005", capability="G",
         q="How many clinical trial participants withdrew from the study?",
         answerable=False),
    dict(id="G-006", capability="G",
         q="What is the total headcount in the engineering org chart?",
         answerable=False),
]


# --------------------------------------------------------------------------------- #
# Emit
# --------------------------------------------------------------------------------- #

def write_tables(out: Path, built: dict) -> None:
    for store, table_names in STORE_TABLES.items():
        (out / "tables" / store).mkdir(parents=True, exist_ok=True)
        for name in table_names:
            columns, rows = built[name]
            path = out / "tables" / store / f"{name}.csv"
            with open(path, "w", newline="") as fh:
                writer = csv.writer(fh)
                writer.writerow(columns)
                writer.writerows(rows)


def check_column_types(out: Path) -> list:
    """Flag any column that is numeric in most rows but blank or textual in a few.

    `gold_value` sniffs a column as REAL only when EVERY body cell parses as a number,
    while the product side coerces cell by cell. A partially-numeric column therefore
    lands as TEXT in the gold engine and REAL in the product engine, and a comparison
    silently stops matching. Better to fail the build than to freeze that."""
    problems = []
    for csv_path in sorted((out / "tables").rglob("*.csv")):
        rows = list(csv.reader(open(csv_path, newline="")))
        header, body = rows[0], rows[1:]
        for i, col in enumerate(header):
            values = [r[i] for r in body]
            numeric = sum(1 for v in values if _is_number(v))
            if 0 < len(values) - numeric <= len(values) * 0.5 and numeric:
                problems.append(
                    f"{csv_path.parent.name}/{csv_path.stem}.{col}: {numeric}/{len(values)} "
                    "cells numeric - mixed column, gold and product engines will disagree")
    return problems


def _is_number(value: str) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def _fact_str(value) -> str:
    """How the executed answer is written into key_facts. Integral floats lose the
    trailing '.0' so the scorer's phrase match sees the number a model would write."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def build_questions(tables: dict) -> tuple:
    """Execute every question's SQL against the independent gold engine and attach the
    result as its key_fact. Returns (questions, derivations) - derivations record the
    provenance of every string answer so the validator can re-execute them."""
    questions, derivations = [], []
    for spec in QUESTIONS:
        item = {
            "id": spec["id"],
            "capability": spec["capability"],
            "question": spec["q"],
            "expect_stores": spec.get("stores", []),
            "hardness": spec.get("hardness", ["plain"]),
            "answerable": spec.get("answerable", True),
            # An unanswerable item is "refused", never "public". Stage 1's amendment
            # 260731a stops routing-scoring a refused item precisely because a question
            # about a system that does not exist can legitimately share vocabulary with
            # a real store and still be answered correctly - with a refusal. Marking
            # these public would fail every G item on routing and bury the one signal
            # they exist to give: did the product decline, or did it invent?
            "protection": "refused" if not spec.get("answerable", True) else "public",
        }
        if spec.get("variant_of"):
            item["variant_of"] = spec["variant_of"]
        if "wrong-vocab" in item["hardness"]:
            item["profiles"] = ["semantic"]

        sql = spec.get("sql") or spec.get("derive")
        if sql:
            value = gold_value(tables, sql)
            if value is None:
                raise SystemExit(f"{spec['id']}: SQL returned NULL - the frozen slice has no "
                                 f"rows for this question:\n    {sql}")
            item["key_facts"] = [_fact_str(value)]
            # `table` records which table the gold query drives from, for a human reading
            # the spec. It is deliberately NOT emitted as gold_table.
            #
            # `table_hit` compares one declared table against citations[].table, but the
            # product is free to reach the same correct answer from either side of a
            # join. B-001 proved it: gold is a single-table AVG over `reviews`, the
            # product wrote `FROM orders INNER JOIN reviews`, cited `orders`, and
            # returned the exactly-correct 3.79 - scored a retrieval MISS. That is the
            # #463 artifact class, and a pack built to escape a self-grading corpus has
            # no business importing it. Store-level traversal is still measured, by
            # routing against expect_stores; arithmetic is measured by execution
            # accuracy. Neither can be satisfied by citing the "wrong" correct table.
            if spec.get("sql"):
                if not _is_number(str(value)):
                    raise SystemExit(f"{spec['id']}: gold_sql must return a NUMERIC scalar "
                                     f"(execution accuracy is numeric-only), got {value!r}")
                item["gold_sql"] = spec["sql"]
            else:
                derivations.append({"id": spec["id"], "sql": sql, "value": _fact_str(value)})
        else:
            item["key_facts"] = []
        questions.append(item)
    return questions, derivations


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build the real-data golden pack (#473).")
    parser.add_argument("--out", default=str(ROOT / "eval_fixtures" / "golden_pack_real"))
    args = parser.parse_args(argv)
    out = Path(args.out)

    dirs = dataset_dirs()
    built = {}
    built.update(build_olist(dirs["olist"]))
    built.update(build_movielens(dirs["movielens"]))
    built.update(build_baseball(dirs["baseball"]))

    (out / "docs").mkdir(parents=True, exist_ok=True)
    (out / "docs" / ".gitkeep").write_text("")
    write_tables(out, built)

    problems = check_column_types(out)
    if problems:
        print("MIXED COLUMNS (fix the projection, do not freeze this):", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    tables = {store: {name: out / "tables" / store / f"{name}.csv" for name in names}
              for store, names in STORE_TABLES.items()}
    questions, derivations = build_questions(tables)

    (out / "questions.jsonl").write_text(
        "".join(json.dumps(q, sort_keys=True) + "\n" for q in questions))
    (out / "pack_meta.json").write_text(json.dumps({
        "provenance": "third-party public datasets, downloaded not authored",
        "sources": DATASETS,
        "slice": {"olist_month": OLIST_MONTH, "olist_stride": OLIST_STRIDE,
                  "movielens_users": MOVIELENS_USERS, "baseball_from": BASEBALL_FROM},
        "answer_key": "every key_fact is the result of executing SQL on an independent "
                      "sqlite engine over these frozen CSVs - no answer is model-authored",
        "built_by": "scripts/build_real_pack.py",
        "stores": {sid: {**meta, "schema_descriptions": SCHEMA_DESCRIPTIONS.get(sid, {})}
                   for sid, meta in STORE_META.items()},
        "alignments": [],
        "derivations": derivations,
    }, indent=2, sort_keys=True) + "\n")

    rows = sum(len(r) for _, r in built.values())
    print(f"pack -> {out}")
    print(f"  {len(STORE_TABLES)} stores, {len(built)} tables, {rows} rows, "
          f"{len(questions)} questions ({len(derivations)} string-answer)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

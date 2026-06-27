"""OSM org_type classification — incl. resale/consignment detection (buy-back shops)."""
from pipeline.osm_ingest import _org_type


def test_resale_chains_classify_as_consignment():
    for name in ["Plato's Closet", "Buffalo Exchange", "Uptown Cheapskate", "Once Upon A Child"]:
        assert _org_type({"shop": "second_hand", "name": name}) == "consignment", name


def test_consignment_keyword_and_shop_tag():
    assert _org_type({"shop": "second_hand", "name": "Lakewood Consignment Boutique"}) == "consignment"
    assert _org_type({"shop": "consignment", "name": "Anything"}) == "consignment"


def test_donation_thrift_stays_thrift():
    assert _org_type({"shop": "second_hand", "name": "Ohio Thrift Store"}) == "thrift_store"
    assert _org_type({"shop": "second_hand", "name": "Village Discount Outlet"}) == "thrift_store"


def test_other_types_unaffected():
    assert _org_type({"shop": "charity", "name": "Goodwill"}) == "charity_store"
    assert _org_type({"amenity": "recycling", "recycling:clothes": "yes"}) == "drop_bin"
    assert _org_type({"shop": "bakery"}) == "other"

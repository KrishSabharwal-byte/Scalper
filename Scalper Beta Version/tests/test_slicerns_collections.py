import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mongo_service import mongo_service


def test_collection_name_resolution():
    assert mongo_service.get_client_collection_name("admin") == "Admin"
    assert mongo_service.get_client_collection_name("Admin") == "Admin"
    assert mongo_service.get_client_collection_name("user1") == "User 1"
    assert mongo_service.get_client_collection_name("User 1") == "User 1"
    assert mongo_service.get_client_collection_name("user_1") == "User 1"
    assert mongo_service.get_client_collection_name("trader_01") == "User 1"
    assert mongo_service.get_client_collection_name("user2") == "User 2"
    assert mongo_service.get_client_collection_name("User 2") == "User 2"
    assert mongo_service.get_client_collection_name("user_2") == "User 2"
    assert mongo_service.get_client_collection_name("trader_02") == "User 2"
    assert mongo_service.get_client_collection_name("user3") == "User 3"
    assert mongo_service.get_client_collection_name("User 3") == "User 3"
    assert mongo_service.get_client_collection_name("user_3") == "User 3"
    assert mongo_service.get_client_collection_name("trader_03") == "User 3"
    assert mongo_service.get_client_collection_name("user4") == "User 4"
    assert mongo_service.get_client_collection_name("User 4") == "User 4"
    assert mongo_service.get_client_collection_name("user_4") == "User 4"
    assert mongo_service.get_client_collection_name("trader_04") == "User 4"


def test_scalper_database_routing():
    from mongo_service import MongoService
    prod_service = MongoService(db_name="Scalper")
    assert prod_service.db_name == "Scalper"
    status_admin = prod_service.get_status("admin")
    assert status_admin["database"] == "Scalper"
    assert status_admin["scoped_collection"] == "Admin"

    status_u1 = prod_service.get_status("user1")
    assert status_u1["database"] == "Scalper"
    assert status_u1["scoped_collection"] == "User 1"

    status_u2 = prod_service.get_status("user2")
    assert status_u2["database"] == "Scalper"
    assert status_u2["scoped_collection"] == "User 2"

    status_u3 = prod_service.get_status("user3")
    assert status_u3["database"] == "Scalper"
    assert status_u3["scoped_collection"] == "User 3"

    status_u4 = prod_service.get_status("user4")
    assert status_u4["database"] == "Scalper"
    assert status_u4["scoped_collection"] == "User 4"


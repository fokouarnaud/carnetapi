"""Routes owned by the catalogue domain; external API paths remain stable."""
from django.urls import include, path
from catalogue.api.catalogue import ProductViewSet
from catalogue.api.price_conflicts import ProductPriceConflictViewSet
from catalogue.api.pack_price_conflicts import PackPriceConflictViewSet
from catalogue.api.packs import PackViewSet
from catalogue.api.categories import CategoryViewSet
from catalogue.api.barcodes import BarcodeAssignmentViewSet, BarcodeLookupViewSet
from catalogue.api.product_photos import ProductPhotoViewSet
from catalogue.api.catalogue_imports import CatalogueImportViewSet

urlpatterns = [
    path("shops/<uuid:shop_id>/products/<uuid:product_id>/packs/<uuid:pack_id>/price-conflicts/", PackPriceConflictViewSet.as_view({"get": "list", "post": "create"}), name="pack-price-conflict-list"),
    path("shops/<uuid:shop_id>/products/<uuid:product_id>/packs/<uuid:pack_id>/price-conflicts/<uuid:pk>/", PackPriceConflictViewSet.as_view({"get": "retrieve"}), name="pack-price-conflict-detail"),
    path("shops/<uuid:shop_id>/products/<uuid:product_id>/packs/<uuid:pack_id>/price-conflicts/<uuid:pk>/proposals/", PackPriceConflictViewSet.as_view({"get": "proposals"}), name="pack-price-conflict-proposals"),
    path("shops/<uuid:shop_id>/products/<uuid:product_id>/packs/<uuid:pack_id>/price-conflicts/<uuid:pk>/preview/", PackPriceConflictViewSet.as_view({"get": "preview"}), name="pack-price-conflict-preview"),
    path("shops/<uuid:shop_id>/products/<uuid:product_id>/packs/<uuid:pack_id>/price-conflicts/<uuid:pk>/resolve/", PackPriceConflictViewSet.as_view({"post": "resolve"}), name="pack-price-conflict-resolve"),
    path("shops/<uuid:shop_id>/products/<uuid:product_id>/price-conflicts/",ProductPriceConflictViewSet.as_view({"get":"list","post":"create"}),name="product-price-conflict-list"),
    path("shops/<uuid:shop_id>/products/<uuid:product_id>/price-conflicts/<uuid:pk>/",ProductPriceConflictViewSet.as_view({"get":"retrieve"}),name="product-price-conflict-detail"),
    path("shops/<uuid:shop_id>/products/<uuid:product_id>/price-conflicts/<uuid:pk>/preview/",ProductPriceConflictViewSet.as_view({"get":"preview"}),name="product-price-conflict-preview"),
    path("shops/<uuid:shop_id>/products/<uuid:product_id>/price-conflicts/<uuid:pk>/resolve/",ProductPriceConflictViewSet.as_view({"post":"resolve"}),name="product-price-conflict-resolve"),
    path("shops/<uuid:shop_id>/customer-imports/", CatalogueImportViewSet.as_view({"post": "create"}, kind="customers"), name="customer-imports-create"),
    path("shops/<uuid:shop_id>/customer-imports/template/", CatalogueImportViewSet.as_view({"get": "template"}, kind="customers"), name="customer-imports-template"),
    path("shops/<uuid:shop_id>/customer-imports/<uuid:pk>/", CatalogueImportViewSet.as_view({"get": "retrieve"}, kind="customers"), name="customer-imports-retrieve"),
    path("shops/<uuid:shop_id>/customer-imports/<uuid:pk>/confirm/", CatalogueImportViewSet.as_view({"post": "confirm"}, kind="customers"), name="customer-imports-confirm"),
    path("shops/<uuid:shop_id>/supplier-imports/", CatalogueImportViewSet.as_view({"post": "create"}, kind="suppliers"), name="supplier-imports-create"),
    path("shops/<uuid:shop_id>/supplier-imports/template/", CatalogueImportViewSet.as_view({"get": "template"}, kind="suppliers"), name="supplier-imports-template"),
    path("shops/<uuid:shop_id>/supplier-imports/<uuid:pk>/", CatalogueImportViewSet.as_view({"get": "retrieve"}, kind="suppliers"), name="supplier-imports-retrieve"),
    path("shops/<uuid:shop_id>/supplier-imports/<uuid:pk>/confirm/", CatalogueImportViewSet.as_view({"post": "confirm"}, kind="suppliers"), name="supplier-imports-confirm"),
    path("shops/<uuid:shop_id>/catalogue-imports/", CatalogueImportViewSet.as_view({"post": "create"}), name="catalogue-import-list"),
    path("shops/<uuid:shop_id>/catalogue-imports/template/", CatalogueImportViewSet.as_view({"get": "template"}), name="catalogue-import-template"),
    path("shops/<uuid:shop_id>/catalogue-imports/<uuid:pk>/confirm/", CatalogueImportViewSet.as_view({"post": "confirm"}), name="catalogue-import-confirm"),
    path("shops/<uuid:shop_id>/catalogue-imports/<uuid:pk>/", CatalogueImportViewSet.as_view({"get": "retrieve"}), name="catalogue-import-detail"),
    path("shops/<uuid:shop_id>/products/<uuid:product_id>/photo/", ProductPhotoViewSet.as_view({"get": "retrieve", "post": "create", "delete": "destroy"}), name="product-photo"),
    path("shops/<uuid:shop_id>/barcode-lookup/", BarcodeLookupViewSet.as_view({"get": "retrieve"}), name="barcode-lookup"),
    path("shops/<uuid:shop_id>/products/<uuid:product_id>/barcode/", BarcodeAssignmentViewSet.as_view({"patch": "partial_update"}), name="product-barcode"),
    path("shops/<uuid:shop_id>/products/<uuid:product_id>/packs/<uuid:pack_id>/barcode/", BarcodeAssignmentViewSet.as_view({"patch": "partial_update"}), name="pack-barcode"),
    path("shops/<uuid:shop_id>/categories/", CategoryViewSet.as_view({"get": "list", "post": "create"}), name="category-list"),
    path("shops/<uuid:shop_id>/categories/<uuid:pk>/", CategoryViewSet.as_view({"get": "retrieve", "patch": "partial_update"}), name="category-detail"),
    path("shops/<uuid:shop_id>/products/", ProductViewSet.as_view({"get": "list", "post": "create"}), name="product-list"),
    path("shops/<uuid:shop_id>/products/<uuid:pk>/", ProductViewSet.as_view({"get": "retrieve", "patch": "partial_update"}), name="product-detail"),
    path("shops/<uuid:shop_id>/products/<uuid:pk>/history/", ProductViewSet.as_view({"get": "history"}), name="product-history"),
    path("shops/<uuid:shop_id>/products/<uuid:product_id>/packs/<uuid:pk>/history/", PackViewSet.as_view({"get": "history"}), name="pack-history"),
    path("shops/<uuid:shop_id>/products/<uuid:product_id>/packs/<uuid:pk>/", PackViewSet.as_view({"get": "retrieve", "patch": "partial_update"}), name="pack-detail"),
    path("shops/<uuid:shop_id>/products/<uuid:product_id>/packs/", PackViewSet.as_view({"get": "list", "post": "create"}), name="pack-list"),
]

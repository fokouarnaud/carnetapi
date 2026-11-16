"""Seeds the suggested SYSCOHADA accounts, frozen here; each is to be validated by the accountant."""
import uuid

from django.db import migrations

SUGGESTIONS = [
    ("method:cash", "571", "Caisse"),
    ("method:mtn", "521", "Monnaie électronique MTN"),
    ("method:orange", "521", "Monnaie électronique Orange"),
    ("method:bank", "521", "Banques"),
    ("method:other", "521", "Autre moyen de paiement"),
    ("sale_payment", "701", "Ventes de marchandises"),
    ("sale_return_refund", "701", "Ventes de marchandises (retours remboursés)"),
    ("customer_collection", "411", "Clients"),
    ("customer_refund", "411", "Clients (remboursement)"),
    ("supplier_payment", "401", "Fournisseurs"),
    ("supplier_refund", "401", "Fournisseurs (remboursement reçu)"),
    ("opening", "108", "Compte de l'exploitant (fonds de caisse initial)"),
    ("addition", "108", "Compte de l'exploitant (apport)"),
    ("withdrawal", "108", "Compte de l'exploitant (retrait)"),
    ("expense:transport", "618", "Autres frais de transport"),
    ("expense:rent", "622", "Locations et charges locatives"),
    ("expense:salaries", "661", "Rémunérations directes versées au personnel"),
    ("expense:supplies", "605", "Autres achats (fournitures)"),
    ("expense:bills", "605", "Autres achats (eau, électricité, factures)"),
    ("expense:other", "658", "Charges diverses"),
    ("count_shortage", "658", "Charges diverses (manquant de caisse)"),
    ("count_surplus", "758", "Produits divers (excédent de caisse)"),
    ("sales", "701", "Ventes de marchandises"),
    ("purchases", "601", "Achats de marchandises"),
    ("customers", "411", "Clients"),
    ("suppliers", "401", "Fournisseurs"),
    ("supplier_advances", "409", "Fournisseurs débiteurs (avances et avoirs)"),
    ("customer_credits", "419", "Clients créditeurs (avoirs et trop-perçus)"),
    ("stock", "311", "Marchandises"),
    ("stock_loss", "6031", "Variation des stocks de marchandises (pertes)"),
]


def seed(apps, schema_editor):
    AccountSuggestion = apps.get_model("accounting", "AccountSuggestion")
    for key, account, label in SUGGESTIONS:
        AccountSuggestion.objects.get_or_create(key=key, defaults={"id": uuid.uuid4(), "account": account, "label": label})


class Migration(migrations.Migration):
    dependencies = [("accounting", "0001_initial")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]

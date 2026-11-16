"""Suggested SYSCOHADA accounts for the accountant's working exports (16 November 2026).

The application does not keep the books: each exported line carries an account
suggested from the nature of the operation, which the accountant validates and
the technical administrator adjusts in Unfold.
"""
from django.core.validators import RegexValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from common.model_base import UUIDRecord

# (key, default account, label); every default must be validated by the accountant.
DEFAULT_SUGGESTIONS = [
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

ACCOUNTS_NOTE = ("Comptes SYSCOHADA suggérés par l'application, à valider par l'expert-comptable "
                 "avant toute écriture.")


class AccountSuggestion(UUIDRecord):
    """The account suggested for one nature of operation in the accountant's exports."""
    key = models.CharField(_("Operation nature"), max_length=40, unique=True, editable=False)
    account = models.CharField(_("Suggested account"), max_length=12,
                               validators=[RegexValidator(r"^[0-9]{2,12}$", _("Digits only, 2 to 12."))])
    label = models.CharField(_("Account label"), max_length=120)

    class Meta:
        ordering = ["key"]
        verbose_name = _("suggested account")
        verbose_name_plural = _("suggested accounts")
        constraints = [models.CheckConstraint(condition=models.Q(account__regex=r"^[0-9]{2,12}$"),
                                              name="account_suggestion_digits")]

    def __str__(self):
        return f"{self.key} → {self.account}"


def suggestions():
    """Key → (account, label), read once per export."""
    return {row.key: (row.account, row.label) for row in AccountSuggestion.objects.all()}

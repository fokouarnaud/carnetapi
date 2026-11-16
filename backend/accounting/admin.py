"""Unfold: the technical administrator adjusts the suggested accounts, never their natures."""
from django.contrib import admin
from unfold.admin import ModelAdmin

from accounting.models import AccountSuggestion


@admin.register(AccountSuggestion)
class AccountSuggestionAdmin(ModelAdmin):
    list_display = ["key", "account", "label", "updated_at"]
    list_editable = ["account", "label"]
    search_fields = ["key", "account", "label"]
    readonly_fields = ["key", "updated_at"]
    fields = ["key", "account", "label", "updated_at"]

    def has_module_permission(self, request):
        return request.user.is_active and request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_superuser

    def has_add_permission(self, request):
        return False  # the natures are fixed by the application

    def has_delete_permission(self, request, obj=None):
        return False

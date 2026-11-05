from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from unfold.admin import ModelAdmin
from django.utils.translation import gettext_lazy as _

from identity.models import Membership, Shop, User


@admin.register(User)
class UserAdmin(DjangoUserAdmin, ModelAdmin):
    fieldsets = DjangoUserAdmin.fieldsets + ((_("Profile"), {"fields": ("phone", "language")}),)


@admin.register(Shop)
class ShopAdmin(ModelAdmin):
    list_display = ["name", "owner", "timezone", "is_active"]
    search_fields = ["name"]
    readonly_fields = ["id", "created_at", "updated_at", "seller_discount_cap_basis_points", "discount_policy_version"]

    # Shop ownership and access must go through an audited business service.
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Membership)
class MembershipAdmin(ModelAdmin):
    list_display = ["shop", "user", "role", "is_active"]
    list_filter = ["role", "is_active"]
    readonly_fields = ["id", "created_at", "updated_at"]

    # Use the audited business service; generic admin writes bypass owner checks.
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

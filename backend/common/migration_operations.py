"""Compatibility operations for the one-time split of the historical core app."""
from django.db.migrations.operations.base import Operation


class RelocateModels(Operation):
    """Move migration state and content types without altering business tables.

    The mapping is frozen in the migration, never inferred from runtime models.
    Content-type IDs and their permission/group links remain unchanged.
    """

    reversible = True
    reduces_to_sql = False

    def __init__(self, destinations):
        self.destinations = destinations

    def deconstruct(self):
        return self.__class__.__name__, [], {"destinations": self.destinations}

    def state_forwards(self, app_label, state):
        relocated = {}
        for (label, name), original in state.models.items():
            model = original.clone()
            # ModelState.clone() deliberately shares fields. Changing relations
            # must not mutate the predecessor state used for reverse migrations.
            model.fields = {key: field.clone() for key, field in original.fields.items()}
            if label == app_label:
                model.options.setdefault("db_table", f"{app_label}_{name}")
                model.app_label = self.destinations.get(name, label)
            for field in model.fields.values():
                remote = field.remote_field
                if remote is None:
                    continue
                for attribute in ("model", "through"):
                    target = getattr(remote, attribute, None)
                    if not isinstance(target, str) or "." not in target:
                        continue
                    target_label, target_name = target.split(".", 1)
                    destination = self.destinations.get(target_name.lower())
                    if target_label == app_label and destination:
                        setattr(remote, attribute, f"{destination}.{target_name}")
            relocated[model.app_label, name] = model
        state.models = relocated
        state._relations = None
        state.__dict__.pop("apps", None)
        state.is_delayed = False

    def _content_types(self, app_label, schema_editor, state, reverse=False):
        ContentType = state.apps.get_model("contenttypes", "ContentType")
        manager = ContentType.objects.using(schema_editor.connection.alias)
        for model, destination in self.destinations.items():
            before, after = (destination, app_label) if reverse else (app_label, destination)
            source = manager.filter(app_label=before, model=model)
            if source.exists():
                if manager.filter(app_label=after, model=model).exists():
                    raise RuntimeError(f"Conflicting content types for {after}.{model}; resolve explicitly before migration.")
                source.update(app_label=after)
        # Runtime and historical registries have independent managers/caches.
        from django.contrib.contenttypes.models import ContentType as RuntimeContentType
        RuntimeContentType.objects.clear_cache()

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        self._content_types(app_label, schema_editor, to_state)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        self._content_types(app_label, schema_editor, from_state, reverse=True)

    def describe(self):
        return "Relocate domain model state and content types, retaining existing tables and IDs"

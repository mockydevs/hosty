import { FormField } from "@/components/form-field";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
/**
 * Schema-driven form renderer (v2/M4): renders a blueprint's pydantic
 * `inputs()` JSON Schema as a form — typed fields, secret flags, defaults —
 * so a new blueprint ships with zero per-blueprint UI code.
 *
 * Supported shapes (everything a blueprint inputs model can declare):
 *   string (secret → password input), string with `enum` (dropdown, e.g. a
 *   registry-sourced version picker), integer/number (min/max), boolean,
 *   object with string additionalProperties (key→value editor, e.g. env or
 *   volumes), and `anyOf [T, null]` optionals (pydantic's `T | None`).
 */
import { Plus, X } from "lucide-react";
import { type FormEvent, useEffect, useState } from "react";

export type JsonSchema = {
  type?: string;
  title?: string;
  description?: string;
  default?: unknown;
  minimum?: number;
  maximum?: number;
  maxLength?: number;
  properties?: Record<string, JsonSchema>;
  required?: string[];
  additionalProperties?: JsonSchema | boolean;
  anyOf?: JsonSchema[];
  /** Allowed values — rendered as a dropdown (e.g. a version picker). */
  enum?: unknown[];
  /** pydantic json_schema_extra={"secret": True} marks write-only secrets. */
  secret?: boolean;
};

type FieldKind = "string" | "number" | "boolean" | "map" | "select";

interface Field {
  name: string;
  kind: FieldKind;
  schema: JsonSchema;
  required: boolean;
}

/** Unwrap pydantic's `T | None` (anyOf [T, {type: null}]) to T. */
function unwrap(schema: JsonSchema): JsonSchema {
  if (schema.anyOf) {
    const inner = schema.anyOf.find((s) => s.type !== "null");
    if (inner) return { ...inner, description: schema.description ?? inner.description };
  }
  return schema;
}

function fieldKind(schema: JsonSchema): FieldKind | null {
  switch (schema.type) {
    case "string":
      return Array.isArray(schema.enum) && schema.enum.length > 0 ? "select" : "string";
    case "integer":
    case "number":
      return "number";
    case "boolean":
      return "boolean";
    case "object":
      return typeof schema.additionalProperties === "object" ? "map" : null;
    default:
      return null;
  }
}

export function schemaFields(schema: JsonSchema): Field[] {
  const required = new Set(schema.required ?? []);
  return Object.entries(schema.properties ?? {}).flatMap(([name, raw]) => {
    const inner = unwrap(raw);
    const kind = fieldKind(inner);
    if (!kind) return [];
    return [{ name, kind, schema: inner, required: required.has(name) }];
  });
}

function labelFor(name: string, schema: JsonSchema): string {
  return schema.title ?? name.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
}

type MapRow = { key: string; value: string };

type FieldState = string | boolean | MapRow[];

function initialState(fields: Field[]): Record<string, FieldState> {
  const state: Record<string, FieldState> = {};
  for (const field of fields) {
    if (field.kind === "boolean") state[field.name] = field.schema.default === true;
    else if (field.kind === "map") state[field.name] = [];
    else if (field.kind === "select") {
      const options = (field.schema.enum ?? []).map(String);
      state[field.name] =
        field.schema.default != null ? String(field.schema.default) : (options[0] ?? "");
    } else state[field.name] = field.schema.default != null ? String(field.schema.default) : "";
  }
  return state;
}

/** Parse + validate one field; returns [error, parsedValue]. */
function parseField(field: Field, raw: FieldState): [string | null, unknown] {
  const label = labelFor(field.name, field.schema);
  switch (field.kind) {
    case "boolean":
      return [null, raw === true];
    case "number": {
      const text = (raw as string).trim();
      if (!text) return field.required ? [`${label} is required`, null] : [null, undefined];
      const value = Number(text);
      if (!Number.isInteger(value)) return [`${label} must be a whole number`, null];
      const { minimum, maximum } = field.schema;
      if (minimum != null && value < minimum) return [`${label} must be ≥ ${minimum}`, null];
      if (maximum != null && value > maximum) return [`${label} must be ≤ ${maximum}`, null];
      return [null, value];
    }
    case "string":
    case "select": {
      const text = (raw as string).trim();
      if (!text) return field.required ? [`${label} is required`, null] : [null, undefined];
      return [null, text];
    }
    case "map": {
      const rows = raw as MapRow[];
      const out: Record<string, string> = {};
      for (const row of rows) {
        const key = row.key.trim();
        if (!key) return [`${label}: every entry needs a name`, null];
        if (key in out) return [`${label}: duplicate entry ${key}`, null];
        out[key] = row.value;
      }
      return [null, out];
    }
  }
}

/** A per-field action (e.g. "Autogenerate" next to a domain). `run` returns
 * the new field value, or null to leave it unchanged (e.g. on error). */
export type FieldAction = { label: string; run: () => Promise<string | null> };

function FieldActionButton({
  action,
  onValue,
}: { action: FieldAction; onValue: (v: string) => void }) {
  const [busy, setBusy] = useState(false);
  return (
    <Button
      type="button"
      variant="outline"
      loading={busy}
      onClick={async () => {
        setBusy(true);
        try {
          const value = await action.run();
          if (value != null) onValue(value);
        } finally {
          setBusy(false);
        }
      }}
    >
      {action.label}
    </Button>
  );
}

function MapEditor({
  id,
  rows,
  onChange,
  keyPlaceholder,
  valuePlaceholder,
  secret,
}: {
  id: string;
  rows: MapRow[];
  onChange: (rows: MapRow[]) => void;
  keyPlaceholder: string;
  valuePlaceholder: string;
  secret?: boolean;
}) {
  return (
    <div className="space-y-2">
      {rows.map((row, i) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: rows have no stable identity until submit
        <div key={i} className="flex items-center gap-2">
          <Input
            aria-label={`${id} name ${i + 1}`}
            placeholder={keyPlaceholder}
            value={row.key}
            autoComplete="off"
            spellCheck={false}
            className="font-mono text-xs"
            onChange={(e) =>
              onChange(rows.map((r, j) => (j === i ? { ...r, key: e.target.value } : r)))
            }
          />
          <Input
            aria-label={`${id} value ${i + 1}`}
            placeholder={valuePlaceholder}
            value={row.value}
            type={secret ? "password" : "text"}
            autoComplete="off"
            spellCheck={false}
            className="font-mono text-xs"
            onChange={(e) =>
              onChange(rows.map((r, j) => (j === i ? { ...r, value: e.target.value } : r)))
            }
          />
          <Button
            type="button"
            variant="ghost"
            size="icon"
            aria-label={`Remove ${id} entry ${i + 1}`}
            onClick={() => onChange(rows.filter((_, j) => j !== i))}
          >
            <X className="h-4 w-4" />
          </Button>
        </div>
      ))}
      <Button
        type="button"
        variant="outline"
        size="sm"
        onClick={() => onChange([...rows, { key: "", value: "" }])}
      >
        <Plus className="h-3.5 w-3.5" aria-hidden /> Add entry
      </Button>
    </div>
  );
}

export function SchemaForm({
  schema,
  onSubmit,
  submitLabel = "Create",
  pending = false,
  serverError,
  fieldActions,
  overrideValues,
  children,
}: {
  schema: JsonSchema;
  onSubmit: (values: Record<string, unknown>) => void | Promise<void>;
  submitLabel?: string;
  pending?: boolean;
  serverError?: string | null;
  /** Per-field action buttons keyed by field name (e.g. domain → Autogenerate). */
  fieldActions?: Record<string, FieldAction>;
  /** Externally computed values (e.g. auto-generated domain). Applied to fields
   *  the user hasn't manually edited yet; user edits always win. */
  overrideValues?: Record<string, string>;
  /** Extra fields rendered above the schema-driven ones (e.g. stack name). */
  children?: React.ReactNode;
}) {
  const fields = schemaFields(schema);
  const [values, setValues] = useState(() => initialState(fields));
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [touched, setTouched] = useState<Set<string>>(() => new Set());

  useEffect(() => {
    if (!overrideValues) return;
    setValues((prev) => {
      const next = { ...prev };
      let changed = false;
      for (const [name, val] of Object.entries(overrideValues)) {
        if (!touched.has(name) && prev[name] !== val) {
          next[name] = val;
          changed = true;
        }
      }
      return changed ? next : prev;
    });
  }, [overrideValues, touched]);

  const set = (name: string, value: FieldState) => {
    setTouched((t) => { const s = new Set(t); s.add(name); return s; });
    setValues((prev) => ({ ...prev, [name]: value }));
  };

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault();
    const out: Record<string, unknown> = {};
    const problems: Record<string, string> = {};
    for (const field of fields) {
      const raw = values[field.name] ?? (field.kind === "map" ? [] : "");
      const [error, value] = parseField(field, raw);
      if (error) problems[field.name] = error;
      else if (value !== undefined) out[field.name] = value;
    }
    setErrors(problems);
    if (Object.keys(problems).length === 0) void onSubmit(out);
  };

  return (
    <form onSubmit={handleSubmit} className="grid gap-6 sm:grid-cols-2" noValidate>
      {children && <div className="sm:col-span-2 space-y-4">{children}</div>}

      {fields.map((field) => {
        const label = labelFor(field.name, field.schema);
        const error = errors[field.name];

        // Maps take full width
        const colSpanClass = field.kind === "map" ? "sm:col-span-2" : "";

        if (field.kind === "boolean") {
          return (
            <label
              key={field.name}
              className={`flex items-start gap-2 text-sm ${colSpanClass}`}
              htmlFor={field.name}
            >
              <input
                id={field.name}
                type="checkbox"
                className="mt-0.5"
                checked={values[field.name] === true}
                onChange={(e) => set(field.name, e.target.checked)}
              />
              <span>
                {label}
                {field.schema.description && (
                  <span className="block text-xs text-muted-foreground">
                    {field.schema.description}
                  </span>
                )}
              </span>
            </label>
          );
        }
        if (field.kind === "map") {
          return (
            <div className={colSpanClass} key={field.name}>
              <FormField label={label} htmlFor={field.name} error={error}>
                {field.schema.description && (
                  <p className="text-xs text-muted-foreground">{field.schema.description}</p>
                )}
                <MapEditor
                  id={field.name}
                  rows={values[field.name] as MapRow[]}
                  onChange={(rows) => set(field.name, rows)}
                  keyPlaceholder="name"
                  valuePlaceholder="value"
                  secret={field.schema.secret}
                />
              </FormField>
            </div>
          );
        }
        if (field.kind === "select") {
          const options = (field.schema.enum ?? []).map(String);
          return (
            <div className={colSpanClass} key={field.name}>
              <FormField label={label} htmlFor={field.name} error={error}>
                <select
                  id={field.name}
                  className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm"
                  value={values[field.name] as string}
                  onChange={(e) => set(field.name, e.target.value)}
                >
                  {options.map((opt) => (
                    <option key={opt} value={opt} className="bg-background text-foreground">
                      {opt}
                    </option>
                  ))}
                </select>
                {field.schema.description && (
                  <p className="text-xs text-muted-foreground">{field.schema.description}</p>
                )}
              </FormField>
            </div>
          );
        }
        const action = fieldActions?.[field.name];
        const input = (
          <Input
            id={field.name}
            type={field.schema.secret ? "password" : field.kind === "number" ? "number" : "text"}
            value={values[field.name] as string}
            autoComplete="off"
            spellCheck={false}
            onChange={(e) => set(field.name, e.target.value)}
          />
        );
        return (
          <div className={colSpanClass} key={field.name}>
            <FormField label={label} htmlFor={field.name} error={error}>
              {action ? (
                <div className="flex items-center gap-2">
                  <div className="flex-1">{input}</div>
                  <FieldActionButton action={action} onValue={(v) => set(field.name, v)} />
                </div>
              ) : (
                input
              )}
              {field.schema.description && (
                <p className="text-xs text-muted-foreground">{field.schema.description}</p>
              )}
            </FormField>
          </div>
        );
      })}

      <div className="sm:col-span-2 pt-2">
        {serverError && (
          <p className="mb-4 text-sm text-destructive" role="alert">
            {serverError}
          </p>
        )}
        <Button type="submit" loading={pending} className="w-full sm:w-auto">
          {submitLabel}
        </Button>
      </div>
    </form>
  );
}

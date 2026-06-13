import { FormField } from "@/components/form-field";
import { type JsonSchema, SchemaForm } from "@/components/schema-form";
import { ErrorState, LoadingState } from "@/components/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogActions,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { api, apiErrorMessage } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
/**
 * Stack create wizard (v2/M4): pick a blueprint, then fill a form rendered
 * straight from the blueprint's declared inputs schema — adding a blueprint
 * to the backend lights it up here with zero UI code. Secrets generated at
 * create are shown exactly once before navigating to the new stack.
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Boxes, Copy } from "lucide-react";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router";
import { toast } from "sonner";

type Blueprint = components["schemas"]["BlueprintResponse"];
type BlueprintWithMeta = Blueprint & {
  category?: string;
  icon?: string;
  display_name?: string;
  description?: string;
};
type Accepted = components["schemas"]["StackOperationAccepted"];

function BlueprintPicker({
  blueprints,
  onPick,
}: {
  blueprints: Blueprint[];
  onPick: (bp: Blueprint) => void;
}) {
  const categories = Array.from(
    new Set(blueprints.map((bp) => (bp as BlueprintWithMeta).category || "Other")),
  );

  return (
    <div className="space-y-10">
      {categories.map((category) => {
        const categoryBlueprints = blueprints.filter(
          (bp) => ((bp as BlueprintWithMeta).category || "Other") === category,
        );
        return (
          <div key={category} className="space-y-4">
            <h2 className="text-xl font-semibold tracking-tight">{category}</h2>
            <div className="grid gap-4 sm:grid-cols-1 md:grid-cols-2 lg:grid-cols-2 xl:grid-cols-3">
              {categoryBlueprints.map((bp) => {
                const schema = bp.inputs_schema as JsonSchema;
                const meta = bp as BlueprintWithMeta;
                return (
                  <Card
                    key={bp.id}
                    className="cursor-pointer hover:border-primary transition-all duration-200 group flex items-start p-4"
                    onClick={() => onPick(bp)}
                  >
                    <div className="mr-4 mt-1 rounded-md bg-muted p-2 group-hover:bg-primary/10 group-hover:text-primary transition-colors">
                      <Boxes className="h-8 w-8" aria-hidden />
                    </div>
                    <div className="flex-1 space-y-1">
                      <div className="flex items-center justify-between">
                        <h3 className="font-semibold leading-none tracking-tight text-base">
                          {meta.display_name || schema.title || bp.id}
                        </h3>
                        <Badge variant="secondary" className="text-[10px]">
                          v{bp.version}
                        </Badge>
                      </div>
                      <p className="text-sm text-muted-foreground line-clamp-2">
                        {meta.description || schema.description || `Deploy ${bp.id}`}
                      </p>
                    </div>
                  </Card>
                );
              })}
            </div>
          </div>
        );
      })}
    </div>
  );
}

export function ShowOnceDialog({
  secrets,
  onClose,
}: {
  secrets: Record<string, string> | null;
  onClose: () => void;
}) {
  const copy = async (value: string) => {
    try {
      await navigator.clipboard.writeText(value);
      toast.success("Copied");
    } catch {
      toast.error("Copy failed — select the text manually");
    }
  };

  return (
    <Dialog open={secrets !== null} onClose={onClose}>
      <DialogContent>
        <DialogTitle>Generated secrets</DialogTitle>
        <DialogDescription>
          Save these now — they are shown only this once and stored encrypted.
        </DialogDescription>
        {secrets && (
          <dl className="space-y-2 text-sm">
            {Object.entries(secrets).map(([label, value]) => (
              <div key={label} className="flex items-center justify-between gap-2">
                <dt className="text-muted-foreground">{label}</dt>
                <dd className="flex items-center gap-1 font-mono text-xs">
                  <span className="break-all">{value}</span>
                  <Button
                    variant="ghost"
                    size="icon"
                    aria-label={`Copy ${label}`}
                    onClick={() => copy(value)}
                  >
                    <Copy className="h-3.5 w-3.5" />
                  </Button>
                </dd>
              </div>
            ))}
          </dl>
        )}
        <DialogActions>
          <Button onClick={onClose}>I saved them</Button>
        </DialogActions>
      </DialogContent>
    </Dialog>
  );
}

export function StackCreatePage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState<Blueprint | null>(null);
  const [name, setName] = useState("");
  const [nameError, setNameError] = useState<string | null>(null);
  const [serverError, setServerError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [accepted, setAccepted] = useState<Accepted | null>(null);

  useEffect(() => {
    if (selected) {
      const shortId = Math.random().toString(36).substring(2, 6);
      setName(`${selected.id}-${shortId}`);
      setNameError(null);
      setServerError(null);
    } else {
      setName("");
    }
  }, [selected]);

  const blueprints = useQuery({
    queryKey: ["stack-blueprints"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/stacks/blueprints");
      if (error || !data) throw new Error(apiErrorMessage(error, "Failed to load blueprints"));
      return data;
    },
  });

  const submit = async (inputs: Record<string, unknown>) => {
    if (!selected) return;
    const slug = name.trim().toLowerCase();
    if (!/^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$/.test(slug)) {
      setNameError("1-32 lowercase letters, digits or hyphens; no leading/trailing hyphen");
      return;
    }
    setNameError(null);
    setServerError(null);
    setPending(true);
    try {
      const { data, error, response } = await api.POST("/api/stacks", {
        body: { name: slug, blueprint_id: selected.id, inputs },
      });
      if (error || !data) {
        setServerError(apiErrorMessage(error, `Create failed (${response.status})`));
        return;
      }
      await queryClient.invalidateQueries({ queryKey: ["stacks"] });
      toast.success(`Deploying ${data.stack.name}…`);
      if (Object.keys(data.show_once ?? {}).length > 0) {
        setAccepted(data);
      } else {
        navigate(`/stacks/${data.stack.id}`, { state: { operationId: data.operation_id } });
      }
    } finally {
      setPending(false);
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <Button
          variant="ghost"
          size="icon"
          aria-label="Back"
          onClick={() => (selected ? setSelected(null) : navigate("/stacks"))}
        >
          <ArrowLeft className="h-4 w-4" />
        </Button>
        <h1 className="text-2xl font-semibold tracking-tight">
          {selected
            ? `New ${(selected.inputs_schema as JsonSchema).title ?? selected.id} stack`
            : "New stack"}
        </h1>
      </div>

      {blueprints.isPending ? (
        <LoadingState label="Loading blueprints…" />
      ) : blueprints.isError ? (
        <ErrorState message={blueprints.error.message} onRetry={() => blueprints.refetch()} />
      ) : !selected ? (
        <BlueprintPicker blueprints={blueprints.data} onPick={setSelected} />
      ) : (
        <Card className="max-w-4xl mx-auto w-full">
          <CardHeader>
            <CardTitle className="text-base">Configure</CardTitle>
            <CardDescription>
              Runs rootless under your own isolated system account, published only on loopback and
              served through Caddy with automatic HTTPS.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <SchemaForm
              key={selected.id}
              schema={selected.inputs_schema as JsonSchema}
              onSubmit={submit}
              submitLabel="Create stack"
              pending={pending}
              serverError={serverError}
            >
              <FormField label="Stack name" htmlFor="stack-name" error={nameError ?? undefined}>
                <Input
                  id="stack-name"
                  placeholder="my-app"
                  autoComplete="off"
                  spellCheck={false}
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                />
              </FormField>
            </SchemaForm>
          </CardContent>
        </Card>
      )}

      <ShowOnceDialog
        secrets={accepted?.show_once ?? null}
        onClose={() => {
          if (accepted) {
            navigate(`/stacks/${accepted.stack.id}`, {
              state: { operationId: accepted.operation_id },
            });
          }
        }}
      />
    </div>
  );
}

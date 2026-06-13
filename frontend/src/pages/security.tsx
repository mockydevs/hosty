import { FormField } from "@/components/form-field";
import { ErrorState, LoadingState } from "@/components/states";
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
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api, apiErrorMessage } from "@/lib/api/client";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Key, KeyRound, Plus, ShieldAlert, Ticket, Trash2 } from "lucide-react";
import type React from "react";
import { useState } from "react";
import { toast } from "sonner";

function SshKeysTab() {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [privateKey, setPrivateKey] = useState("");
  const [publicKey, setPublicKey] = useState("");
  const [error, setError] = useState<string | null>(null);

  const keys = useQuery({
    queryKey: ["security", "keys"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/security/keys");
      if (error || !data) throw new Error(apiErrorMessage(error, "Failed to load SSH keys"));
      return data;
    },
  });

  const addKey = useMutation({
    mutationFn: async () => {
      const { data, error, response } = await api.POST("/api/security/keys", {
        body: { name, private_key: privateKey, public_key: publicKey },
      });
      if (error) {
        throw new Error(apiErrorMessage(error, `Failed to add key (${response.status})`));
      }
      return data;
    },
    onSuccess: async () => {
      toast.success("SSH Key added successfully");
      setOpen(false);
      setName("");
      setPrivateKey("");
      setPublicKey("");
      await queryClient.invalidateQueries({ queryKey: ["security", "keys"] });
    },
    onError: (err) => setError(err.message),
  });

  const deleteKey = useMutation({
    mutationFn: async (id: number) => {
      const { error } = await api.DELETE("/api/security/keys/{key_id}", {
        params: { path: { key_id: id } },
      });
      if (error) throw new Error(apiErrorMessage(error, "Failed to delete key"));
    },
    onSuccess: async () => {
      toast.success("SSH Key deleted");
      await queryClient.invalidateQueries({ queryKey: ["security", "keys"] });
    },
    onError: (err) => toast.error(err.message),
  });

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="text-lg font-medium">Private SSH Keys</h3>
        <Button onClick={() => setOpen(true)} className="gap-2">
          <Plus className="h-4 w-4" /> Add Key
        </Button>
        <Dialog open={open} onClose={() => setOpen(false)}>
          <DialogContent className="sm:max-w-[600px]">
            <DialogTitle>Add SSH Key</DialogTitle>
            <DialogDescription>
              Add a private SSH key. This key will be automatically configured so that any of your
              stacks can use it to clone private repositories.
            </DialogDescription>
            <form
              className="space-y-4"
              onSubmit={(e: React.FormEvent) => {
                e.preventDefault();
                addKey.mutate();
              }}
            >
              <FormField label="Name (Identifier)" htmlFor="key-name" error={error ?? undefined}>
                <Input
                  id="key-name"
                  placeholder="e.g. github-deploy-key"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                />
              </FormField>
              <FormField label="Private Key" htmlFor="private-key">
                <textarea
                  id="private-key"
                  placeholder="-----BEGIN OPENSSH PRIVATE KEY-----..."
                  rows={8}
                  className="flex min-h-[80px] w-full rounded-md border border-input bg-transparent px-3 py-2 text-sm ring-offset-background placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50 font-mono text-xs"
                  value={privateKey}
                  onChange={(e: React.ChangeEvent<HTMLTextAreaElement>) =>
                    setPrivateKey(e.target.value)
                  }
                />
              </FormField>
              <FormField label="Public Key (Optional)" htmlFor="public-key">
                <textarea
                  id="public-key"
                  placeholder="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5..."
                  rows={3}
                  className="flex min-h-[80px] w-full rounded-md border border-input bg-transparent px-3 py-2 text-sm ring-offset-background placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50 font-mono text-xs"
                  value={publicKey}
                  onChange={(e: React.ChangeEvent<HTMLTextAreaElement>) =>
                    setPublicKey(e.target.value)
                  }
                />
              </FormField>
              <DialogActions>
                <Button variant="outline" type="button" onClick={() => setOpen(false)}>
                  Cancel
                </Button>
                <Button type="submit" loading={addKey.isPending} disabled={!name || !privateKey}>
                  Save Key
                </Button>
              </DialogActions>
            </form>
          </DialogContent>
        </Dialog>
      </div>

      {keys.isPending ? (
        <LoadingState label="Loading SSH keys..." />
      ) : keys.isError ? (
        <ErrorState message={keys.error.message} onRetry={() => keys.refetch()} />
      ) : keys.data.length === 0 ? (
        <Card className="border-dashed shadow-none">
          <CardContent className="flex flex-col items-center justify-center p-6 text-center">
            <KeyRound className="mb-4 h-8 w-8 text-muted-foreground/50" />
            <h4 className="font-medium">No SSH Keys</h4>
            <p className="mb-4 mt-1 text-sm text-muted-foreground max-w-sm">
              Add an SSH key to securely deploy code from private Git repositories.
            </p>
            <Button variant="outline" onClick={() => setOpen(true)}>
              Add your first key
            </Button>
          </CardContent>
        </Card>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
          {keys.data.map((k) => (
            <Card key={k.id}>
              <CardHeader className="pb-3">
                <div className="flex items-start justify-between">
                  <div className="space-y-1">
                    <CardTitle className="text-base flex items-center gap-2">
                      <Key className="h-4 w-4 text-muted-foreground" />
                      {k.name}
                    </CardTitle>
                    <CardDescription>
                      Added {new Date(k.created_at).toLocaleDateString()}
                    </CardDescription>
                  </div>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-8 w-8 text-destructive opacity-80 hover:opacity-100"
                    onClick={() => {
                      if (confirm(`Delete key ${k.name}?`)) {
                        deleteKey.mutate(k.id);
                      }
                    }}
                    disabled={deleteKey.isPending}
                  >
                    <Trash2 className="h-4 w-4" />
                  </Button>
                </div>
              </CardHeader>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}

function ApiTokensTab() {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [newToken, setNewToken] = useState<string | null>(null);

  const tokens = useQuery({
    queryKey: ["security", "tokens"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/security/tokens");
      if (error || !data) throw new Error(apiErrorMessage(error, "Failed to load tokens"));
      return data;
    },
  });

  const createToken = useMutation({
    mutationFn: async () => {
      const { data, error, response } = await api.POST("/api/security/tokens", {
        body: { name },
      });
      if (error || !data) {
        throw new Error(apiErrorMessage(error, `Failed to create token (${response?.status})`));
      }
      return data;
    },
    onSuccess: async (data: { token: string }) => {
      setNewToken(data.token);
      setOpen(false);
      setName("");
      await queryClient.invalidateQueries({ queryKey: ["security", "tokens"] });
    },
    onError: (err) => setError(err.message),
  });

  const revokeToken = useMutation({
    mutationFn: async (id: number) => {
      const { error } = await api.DELETE("/api/security/tokens/{token_id}", {
        params: { path: { token_id: id } },
      });
      if (error) throw new Error(apiErrorMessage(error, "Failed to revoke token"));
    },
    onSuccess: async () => {
      toast.success("Token revoked");
      await queryClient.invalidateQueries({ queryKey: ["security", "tokens"] });
    },
    onError: (err) => toast.error(err.message),
  });

  return (
    <div className="space-y-4">
      {newToken && (
        <div className="rounded-lg border bg-amber-500/10 text-amber-600 border-amber-500/20 p-4 relative w-full">
          <div className="flex gap-2">
            <ShieldAlert className="h-5 w-5" color="currentColor" />
            <div className="flex-1">
              <h5 className="mb-1 font-medium leading-none tracking-tight">
                Save your new API Token!
              </h5>
              <div className="text-sm opacity-90 mt-2 space-y-2">
                This token will <strong>never be shown again</strong>. Please copy it immediately.
                <div className="mt-2 flex items-center gap-2">
                  <code className="flex-1 rounded bg-black/10 px-2 py-1.5 font-mono text-sm break-all">
                    {newToken}
                  </code>
                  <Button
                    variant="outline"
                    size="sm"
                    className="border-amber-600/30 hover:bg-amber-600/10"
                    onClick={async () => {
                      await navigator.clipboard.writeText(newToken);
                      toast.success("Token copied to clipboard");
                    }}
                  >
                    Copy
                  </Button>
                </div>
                <div className="pt-2">
                  <Button size="sm" variant="ghost" onClick={() => setNewToken(null)}>
                    I've saved it
                  </Button>
                </div>
              </div>
            </div>
          </div>
        </div>
      )}

      <div className="flex items-center justify-between">
        <h3 className="text-lg font-medium">Personal API Tokens</h3>
        <Button onClick={() => setOpen(true)} className="gap-2">
          <Plus className="h-4 w-4" /> Generate Token
        </Button>
        <Dialog open={open} onClose={() => setOpen(false)}>
          <DialogContent>
            <DialogTitle>Generate API Token</DialogTitle>
            <DialogDescription>
              Create a token to authenticate with the HostyPanel REST API from external scripts.
            </DialogDescription>
            <form
              className="space-y-4"
              onSubmit={(e: React.FormEvent) => {
                e.preventDefault();
                createToken.mutate();
              }}
            >
              <FormField label="Token Name" htmlFor="token-name" error={error ?? undefined}>
                <Input
                  id="token-name"
                  placeholder="e.g. CI/CD Pipeline"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                />
              </FormField>
              <DialogActions>
                <Button variant="outline" type="button" onClick={() => setOpen(false)}>
                  Cancel
                </Button>
                <Button type="submit" loading={createToken.isPending} disabled={!name}>
                  Generate
                </Button>
              </DialogActions>
            </form>
          </DialogContent>
        </Dialog>
      </div>

      {tokens.isPending ? (
        <LoadingState label="Loading tokens..." />
      ) : tokens.isError ? (
        <ErrorState message={tokens.error.message} onRetry={() => tokens.refetch()} />
      ) : tokens.data.length === 0 ? (
        <Card className="border-dashed shadow-none">
          <CardContent className="flex flex-col items-center justify-center p-6 text-center">
            <Ticket className="mb-4 h-8 w-8 text-muted-foreground/50" />
            <h4 className="font-medium">No API Tokens</h4>
            <p className="mb-4 mt-1 text-sm text-muted-foreground max-w-sm">
              Generate tokens to interact with the HostyPanel API programmatically.
            </p>
          </CardContent>
        </Card>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
          {tokens.data.map((t) => (
            <Card key={t.id}>
              <CardHeader className="pb-3">
                <div className="flex items-start justify-between">
                  <div className="space-y-1">
                    <CardTitle className="text-base flex items-center gap-2">
                      <Ticket className="h-4 w-4 text-muted-foreground" />
                      {t.name}
                    </CardTitle>
                    <CardDescription>
                      Created {new Date(t.created_at).toLocaleDateString()}
                    </CardDescription>
                  </div>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-8 w-8 text-destructive opacity-80 hover:opacity-100"
                    onClick={() => {
                      if (confirm(`Revoke token ${t.name}?`)) {
                        revokeToken.mutate(t.id);
                      }
                    }}
                    disabled={revokeToken.isPending}
                  >
                    <Trash2 className="h-4 w-4" />
                  </Button>
                </div>
              </CardHeader>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}

export function SecurityPage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-semibold tracking-tight">Security</h1>
        <p className="text-muted-foreground mt-1">Manage your keys, tokens, and credentials.</p>
      </div>

      <Tabs defaultValue="keys" className="w-full">
        <TabsList className="mb-4">
          <TabsTrigger value="keys">Private Keys</TabsTrigger>
          <TabsTrigger value="tokens">API Tokens</TabsTrigger>
        </TabsList>
        <TabsContent value="keys">
          <SshKeysTab />
        </TabsContent>
        <TabsContent value="tokens">
          <ApiTokensTab />
        </TabsContent>
      </Tabs>
    </div>
  );
}

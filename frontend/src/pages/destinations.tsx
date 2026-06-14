import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api/client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { Badge } from "@/components/ui/badge";
import { Plus, Trash2, Server as ServerIcon } from "lucide-react";
import { toast } from "sonner";
import { ErrorState, LoadingState } from "@/components/states";

export function DestinationsPage() {
  const queryClient = useQueryClient();
  const [isOpen, setIsOpen] = useState(false);
  const [name, setName] = useState("");
  const [hostname, setHostname] = useState("");
  const [port, setPort] = useState("22");
  const [sshUser, setSshUser] = useState("root");

  const { data: servers, isLoading, error, refetch } = useQuery({
    queryKey: ["servers"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/servers");
      if (error) throw new Error("Failed to fetch servers");
      return data; /* as any */
    },
  });

  const createServer = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/servers", {
        body: {
          name,
          hostname,
          port: parseInt(port, 10),
          ssh_user: sshUser,
          is_localhost: false,
          ssh_key_id: null,
        },
      });
      if (error) throw new Error("Failed to create server");
      return data; /* as any */
    },
    onSuccess: () => {
      toast.success("Server added");
      setIsOpen(false);
      setName("");
      setHostname("");
      setPort("22");
      setSshUser("root");
      queryClient.invalidateQueries({ queryKey: ["servers"] });
    },
    onError: (error) => {
      toast.error(error.message);
    },
  });

  const deleteServer = useMutation({
    mutationFn: async (id: number) => {
      const { error } = await api.DELETE("/api/servers/{server_id}", {
        params: { path: { server_id: id } },
      });
      if (error) throw new Error("Failed to delete server");
    },
    onSuccess: () => {
      toast.success("Server deleted");
      queryClient.invalidateQueries({ queryKey: ["servers"] });
    },
  });

  if (isLoading) return <LoadingState />;
  if (error) return <ErrorState message={error instanceof Error ? error.message : String(error)} onRetry={refetch} />;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Destinations</h1>
          <p className="text-sm text-muted-foreground">Manage remote servers and deployment targets.</p>
        </div>
        <Button onClick={() => setIsOpen(true)}>
          <Plus className="mr-2 h-4 w-4" /> Add Server
        </Button>
        <Dialog open={isOpen} onClose={() => setIsOpen(false)}>
          <DialogContent>
            <DialogTitle>Add Remote Server</DialogTitle>
            <div className="space-y-4 pt-4">
              <div className="space-y-2">
                <Label>Name</Label>
                <Input placeholder="production-aws-1" value={name} onChange={(e) => setName(e.target.value)} />
              </div>
              <div className="space-y-2">
                <Label>Hostname / IP</Label>
                <Input placeholder="192.168.1.100" value={hostname} onChange={(e) => setHostname(e.target.value)} />
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div className="space-y-2">
                  <Label>SSH Port</Label>
                  <Input type="number" value={port} onChange={(e) => setPort(e.target.value)} />
                </div>
                <div className="space-y-2">
                  <Label>SSH User</Label>
                  <Input value={sshUser} onChange={(e) => setSshUser(e.target.value)} />
                </div>
              </div>
              <Button className="w-full" onClick={() => createServer.mutate()} disabled={createServer.isPending}>
                Add Server
              </Button>
            </div>
          </DialogContent>
        </Dialog>
      </div>

      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
        {servers?.map((server) => (
          <div key={server.id} className="rounded-xl border bg-card text-card-foreground shadow flex flex-col">
            <div className="p-6 flex flex-row items-center justify-between space-y-0 pb-2">
              <h3 className="tracking-tight text-sm font-medium flex items-center gap-2">
                <ServerIcon className="h-4 w-4 text-muted-foreground" />
                {server.name}
              </h3>
              <Badge variant={server.status === "healthy" ? "default" : "destructive"}>
                {server.status}
              </Badge>
            </div>
            <div className="p-6 pt-0 flex-1">
              <div className="text-xs text-muted-foreground mb-4">
                {server.ssh_user}@{server.hostname}:{server.port}
              </div>
              
              {server.os_info && (
                <div className="space-y-2 text-sm">
                  <div className="flex justify-between">
                    <span className="text-muted-foreground">OS</span>
                    <span className="font-medium">{server.os_info.split(' ')[0]}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-muted-foreground">Resources</span>
                    <span className="font-medium">
                      {server.cpu_count} CPU • {server.memory_mb ? Math.round(server.memory_mb / 1024) : 0} GB RAM
                    </span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-muted-foreground">Storage</span>
                    <span className="font-medium">{server.disk_free_gb} GB Free</span>
                  </div>
                </div>
              )}
              
            </div>
            <div className="p-4 border-t flex justify-end">
              <Button 
                variant="ghost" 
                size="sm" 
                className="text-destructive hover:bg-destructive/10 hover:text-destructive"
                onClick={() => deleteServer.mutate(server.id)}
                disabled={server.is_localhost}
              >
                <Trash2 className="h-4 w-4 mr-2" />
                Remove
              </Button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

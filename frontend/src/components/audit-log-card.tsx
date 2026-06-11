import { ErrorState, LoadingState } from "@/components/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { api, apiErrorMessage } from "@/lib/api/client";
/** Week 22: recent audit entries (who did what, when) on the Settings page. */
import { useQuery } from "@tanstack/react-query";
import { ScrollText } from "lucide-react";
import { useState } from "react";

const PAGE_SIZE = 25;

function statusVariant(code: number): "success" | "secondary" | "destructive" {
  if (code < 300) return "success";
  if (code < 500) return "secondary";
  return "destructive";
}

export function AuditLogCard() {
  const [offset, setOffset] = useState(0);
  const audit = useQuery({
    queryKey: ["audit", offset],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/audit", {
        params: { query: { limit: PAGE_SIZE, offset } },
      });
      if (error || !data) throw new Error(apiErrorMessage(error, "Failed to load audit log"));
      return data;
    },
  });

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 text-base">
          <ScrollText className="h-4 w-4" aria-hidden /> Audit log
        </CardTitle>
        <CardDescription>
          Every change made through the panel — who, what, when. Request contents are never stored.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {audit.isPending ? (
          <LoadingState label="Loading audit log…" />
        ) : audit.isError ? (
          <ErrorState message={audit.error.message} onRetry={() => audit.refetch()} />
        ) : audit.data.entries.length === 0 ? (
          <p className="py-6 text-center text-sm text-muted-foreground">No entries yet.</p>
        ) : (
          <>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>When</TableHead>
                  <TableHead>Who</TableHead>
                  <TableHead>Action</TableHead>
                  <TableHead>Result</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {audit.data.entries.map((entry) => (
                  <TableRow key={entry.id}>
                    <TableCell className="whitespace-nowrap text-xs text-muted-foreground">
                      {new Date(entry.created_at).toLocaleString()}
                    </TableCell>
                    <TableCell className="text-sm">
                      {entry.username ?? <span className="text-muted-foreground">anonymous</span>}
                    </TableCell>
                    <TableCell className="font-mono text-xs">
                      {entry.method} {entry.path}
                    </TableCell>
                    <TableCell>
                      <Badge variant={statusVariant(entry.status_code)}>{entry.status_code}</Badge>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            <div className="flex items-center justify-between text-sm text-muted-foreground">
              <span>
                {offset + 1}–{Math.min(offset + PAGE_SIZE, audit.data.total)} of {audit.data.total}
              </span>
              <div className="flex gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={offset === 0}
                  onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                >
                  Newer
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={offset + PAGE_SIZE >= audit.data.total}
                  onClick={() => setOffset(offset + PAGE_SIZE)}
                >
                  Older
                </Button>
              </div>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}

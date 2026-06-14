import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ExternalLink } from "lucide-react";

export function LinksTab() {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Generated Links</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <a href="#" className="flex items-center gap-2 p-4 border rounded-md hover:bg-accent transition-colors">
          <ExternalLink className="h-5 w-5 text-muted-foreground" />
          <span className="font-medium">Public URL</span>
        </a>
      </CardContent>
    </Card>
  );
}

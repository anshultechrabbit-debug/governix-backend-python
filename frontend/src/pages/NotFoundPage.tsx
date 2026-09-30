import { Link } from "react-router";
import { Button, EmptyState } from "../components/ui";

export function NotFoundPage() {
  return (
    <EmptyState
      title="Page not found"
      description="This page does not exist or you do not have access to it."
      action={<Link to="/"><Button variant="secondary">Back to dashboard</Button></Link>}
    />
  );
}

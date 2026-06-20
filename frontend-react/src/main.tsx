import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { Authenticator } from "@aws-amplify/ui-react";
import "@aws-amplify/ui-react/styles.css";
import { configureAmplify } from "./auth/amplifyConfig";
import { OrgGate } from "./auth/OrgGate";
import App from "./App";

configureAmplify();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Authenticator>
      <OrgGate>
        <App />
      </OrgGate>
    </Authenticator>
  </StrictMode>,
);

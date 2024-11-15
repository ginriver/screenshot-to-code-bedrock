import { Stack } from "./lib/stacks";
import { CodeGenerationModel } from "./lib/models";

export enum EditorTheme {
  ESPRESSO = "espresso",
  COBALT = "cobalt",
}

export interface Settings {
  openAiApiKey: string | null;
  openAiBaseURL: string | null;
  anthropicApiKey: string | null;
  awsAccessKey: string | null;
  awsSecretKey: string | null;
  awsRegion: string | null;
  screenshotOneApiKey: string | null;
  isImageGenerationEnabled: boolean;
  editorTheme: EditorTheme;
  generatedCodeConfig: Stack;
  codeGenerationModel: CodeGenerationModel;
  isTermOfServiceAccepted: boolean;
}

export enum AppState {
  INITIAL = "INITIAL",
  CODING = "CODING",
  CODE_READY = "CODE_READY",
}

export enum ScreenRecorderState {
  INITIAL = "initial",
  RECORDING = "recording",
  FINISHED = "finished",
}

export interface CodeGenerationParams {
  generationType: "create" | "update";
  inputMode: "image" | "video";
  image: string;
  resultImage?: string;
  history?: string[];
  isImportedFromCode?: boolean;
}

export type FullGenerationSettings = CodeGenerationParams & Settings;
